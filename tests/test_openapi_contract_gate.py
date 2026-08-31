"""Ten machine gates over the checked-in OpenAPI declaration snapshot.

What this file is for
---------------------
``app/core/openapi_contract.OPERATION_CONTRACTS`` is a hand-curated statement about
what each of the 28 operations really returns. A hand-curated statement is worth
exactly as much as the machinery that keeps it honest, so every gate below is
written to *fail* when the statement stops matching the running application:

===== ==========================================================================
G1    Coverage, fail-closed: served routes and declarations are the same set.
G2    Exactly one 2xx per operation, and it is the declared success status.
G3    No broad buckets anywhere in the served document (`default`, `4XX`, ...).
G4    Declared failures are a superset of what the static scanner can find.
G5    `x-lending-unknown-query-parameters` equals the mechanical route verdict.
G6    Every declared required response header is proven by a real response.
G7    The declared error media type equals the one really served.
G8    Authentication wins over validation; the request-target budget wins over
      authentication.
G9    Injected downstream faults land on a declared status, keep their headers,
      and leave zero business side effects.
G10   The document is structurally valid OpenAPI 3.1 and matches the checked-in
      digest.
===== ==========================================================================

Design rule followed throughout: **a gate may not ask the registry to confirm
itself**. G1 enumerates routes with its own walk of ``app.routes`` rather than
reusing the generator's enumeration; G6/G7/G8/G9 fire real requests through
``TestClient`` and read the real responses. Where a gate depends on evidence
(G6), the absence of evidence is itself a failure, so a newly declared header
cannot pass by never being probed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import func, select

from app.clients.wedap import WedapError
from app.core.config import Settings
from app.core.openapi_contract import (
    OPERATION_CONTRACTS,
    PROBLEM_COMPLIANCE_EXCEPTION_REF,
    UNIVERSAL_RESPONSE_HEADERS,
    OpenApiContractError,
    OperationContract,
    apply_contracts,
    CHALLENGE_APIKEY,
    CHALLENGE_S2S,
    authentication_challenge,
    build_openapi,
    iter_operations,
)
from app.core.openapi_status_scan import scan_app
from app.main import create_app
from app.models.base import Base

# ── shared helpers ───────────────────────────────────────────────────────────

#: Any request-target above 8,192 bytes must be answered 414 before routing or
#: authentication (context.RequestTargetLimitMiddleware). Used as the universal
#: "produce a real error response on every operation" probe: it needs no valid
#: body, no credentials and no database.
OVERLONG_QUERY = "probe=" + "a" * 9000

#: Callback api key handed to the probe app so the two inbound wedap paths really
#: answer 401 instead of degrading to pass-through (see S2SMiddleware.dispatch).
PROBE_CALLBACK_KEY = "probe-callback-key"

DOCS_DIR = Path(__file__).resolve().parent.parent / "docs" / "api-contract"
OPENAPI_JSON = DOCS_DIR / "openapi.json"
OPENAPI_SHA256 = DOCS_DIR / "openapi.sha256"


def canonical_bytes(spec: dict[str, Any]) -> bytes:
    """The one byte-for-byte serialization G10 and the generator script agree on."""
    return (json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def served_routes(app: FastAPI) -> list[tuple[str, str, APIRoute]]:
    """Enumerate schema operations by walking ``app.routes`` directly.

    Deliberately independent of ``openapi_status_scan.route_targets`` (which the
    generator uses): if both sides shared one enumeration, G1 could only prove the
    registry agrees with itself. FastAPI stores an ``_IncludedRouter`` wrapper per
    ``include_router`` call rather than the individual routes, so the walk descends
    through ``original_router`` and carries the include prefix down.
    """
    out: list[tuple[str, str, APIRoute]] = []

    def walk(routes: list[Any], prefix: str) -> None:
        for route in routes:
            original = getattr(route, "original_router", None)
            if original is not None:
                nested = prefix + getattr(route.include_context, "prefix", "")
                walk(list(original.routes), nested)
                continue
            if isinstance(route, APIRoute) and route.include_in_schema:
                for method in sorted(route.methods or set()):
                    if method in {"HEAD", "OPTIONS"}:
                        continue
                    out.append((method, prefix + route.path_format, route))

    walk(list(app.routes), "")
    return out


def concrete(path: str) -> str:
    """Templated path -> a concrete request-target (`{outbox_id}` -> `1`)."""
    return re.sub(r"\{[^}]+\}", "1", path)


def iter_response_maps(node: Any) -> Iterator[dict[str, Any]]:
    """Every ``responses`` object anywhere in the document."""
    if isinstance(node, dict):
        responses = node.get("responses")
        if isinstance(responses, dict):
            yield responses
        for value in node.values():
            yield from iter_response_maps(value)
    elif isinstance(node, list):
        for item in node:
            yield from iter_response_maps(item)


def iter_refs(node: Any) -> Iterator[str]:
    """Every ``$ref`` string anywhere in the document."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            yield ref
        for value in node.values():
            yield from iter_refs(value)
    elif isinstance(node, list):
        for item in node:
            yield from iter_refs(item)


async def create_tables(engine: Any) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def row_counts(session_factory: Any) -> dict[str, int]:
    """Row count of every mapped table -- the zero-side-effect yardstick for G9."""
    counts: dict[str, int] = {}
    async with session_factory() as session:
        for table in Base.metadata.sorted_tables:
            result = await session.execute(select(func.count()).select_from(table))
            counts[table.name] = int(result.scalar_one())
    return counts


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def contract_app() -> FastAPI:
    return create_app()


@pytest.fixture()
def spec(contract_app: FastAPI) -> dict[str, Any]:
    return build_openapi(contract_app)


@pytest.fixture(scope="module")
def probes() -> dict[tuple[str, str, int], httpx.Response]:
    """Real responses, keyed ``(method, path, status)``, for G6/G7 to read.

    Four families, each chosen because it can be produced for its operations
    without a database, valid credentials or a live upstream:

    * ``414`` on all 28 operations -- the request-target budget middleware answers
      before routing, so it works for every method and path shape.
    * ``401`` on all 24 authenticated operations -- omit ``X-Caller-Service`` (S2S
      paths) or ``apikey`` (the two callback paths, which is why this app is built
      with a callback key configured).
    * ``503`` on ``/readyz`` -- unwire ``session_factory``.
    * ``500`` on ``GET /api/v1/deposit/accounts`` -- an upstream client that raises
      a non-``HTTPException``. This is the one status whose headers are re-applied
      by the catch-all handler outside ``IdentifierMiddleware``, so it has to be
      probed rather than assumed.
    """
    app = create_app(Settings(wedap_callback_api_key=PROBE_CALLBACK_KEY))
    client = TestClient(app)
    collected: dict[tuple[str, str, int], httpx.Response] = {}

    for method, path, _route in served_routes(app):
        target = concrete(path)
        collected[(method, path, 414)] = client.request(method, f"{target}?{OVERLONG_QUERY}")
        if 401 in OPERATION_CONTRACTS[(method, path)].failure_statuses:
            collected[(method, path, 401)] = client.request(method, target)

    ready_app = create_app()
    ready_app.state.session_factory = None
    collected[("GET", "/readyz", 503)] = TestClient(ready_app).get("/readyz")

    boom_app = create_app()
    wedap = AsyncMock()
    wedap.get_deposit_accounts.side_effect = ValueError("probe: unmapped exception")
    boom_app.state.wedap = wedap
    collected[("GET", "/api/v1/deposit/accounts", 500)] = TestClient(
        boom_app, raise_server_exceptions=False
    ).get(
        "/api/v1/deposit/accounts",
        headers={"X-Caller-Service": "probe", "X-Tenant-Id": "T1", "X-Request-Id": "probe-req"},
    )
    return collected


@pytest.fixture()
def fault_client() -> tuple[TestClient, AsyncMock, Any]:
    """An app whose upstream is a mock and whose sqlite tables really exist.

    Hands back the session factory alongside the client so the side-effect
    assertions read the very database the request would have written to.
    """
    app = create_app()
    asyncio.run(create_tables(app.state.engine))
    wedap = AsyncMock()
    app.state.wedap = wedap
    return TestClient(app, raise_server_exceptions=False), wedap, app.state.session_factory


FAULT_HEADERS = {
    "X-Caller-Service": "gate-probe",
    "X-Tenant-Id": "T-GATE",
    "X-Request-Id": "req-gate-fault",
}


# ── G1 · coverage, fail-closed ───────────────────────────────────────────────


def test_g1_registry_and_served_routes_are_the_same_set(contract_app: FastAPI) -> None:
    """Every served operation is declared, and every declaration is still served.

    Deliberately does not touch the generated document: this comparison must stay
    answerable even when the generator refuses to build, which is exactly what it
    does when the two sets disagree.
    """
    walked = {(method, path) for method, path, _ in served_routes(contract_app)}
    declared = set(OPERATION_CONTRACTS)

    assert walked - declared == set(), "served but undeclared -- declare it in OPERATION_CONTRACTS"
    assert declared - walked == set(), "declared but no longer served -- delete the stale entry"
    assert len(declared) == 28, f"operation count changed: {len(declared)}"


def test_g1_generated_document_covers_the_same_operations(
    contract_app: FastAPI, spec: dict[str, Any]
) -> None:
    """And the document actually served carries exactly those operations."""
    declared = set(OPERATION_CONTRACTS)
    generated = {(method.upper(), path) for path, item in spec["paths"].items() for method in item}
    assert generated == declared, "the served document disagrees with the registry"
    assert set(iter_operations(contract_app)) == declared


def test_g1_undeclared_route_fails_closed(contract_app: FastAPI) -> None:
    """A route the registry does not know about must raise, never be waved through."""
    schema = build_openapi(contract_app)
    tampered = json.loads(json.dumps(schema))
    tampered["paths"]["/api/v1/not-declared"] = {"get": {"responses": {"200": {}}}}
    with pytest.raises(OpenApiContractError, match="absent from OPERATION_CONTRACTS"):
        apply_contracts(contract_app, tampered)


def test_g1_stale_declaration_fails_closed(
    contract_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A declaration whose route disappeared must raise too."""
    stale = dict(OPERATION_CONTRACTS)
    stale[("GET", "/api/v1/removed-yesterday")] = OPERATION_CONTRACTS[("GET", "/healthz")]
    monkeypatch.setattr("app.core.openapi_contract.OPERATION_CONTRACTS", stale)
    with pytest.raises(OpenApiContractError, match="no longer serves"):
        apply_contracts(contract_app, FastAPI.openapi(contract_app))


# ── G2 · exactly one success status ──────────────────────────────────────────


def test_g2_every_operation_declares_exactly_one_success_status(spec: dict[str, Any]) -> None:
    for path, item in spec["paths"].items():
        for method, operation in item.items():
            contract = OPERATION_CONTRACTS[(method.upper(), path)]
            successes = sorted(
                code for code in operation["responses"] if code.isdigit() and code.startswith("2")
            )
            assert successes == [str(contract.success_status)], (
                f"{method.upper()} {path}: expected exactly one 2xx "
                f"({contract.success_status}), got {successes}"
            )


def test_g2_failure_sets_contain_no_success_status() -> None:
    """The registry itself must not smuggle a second 2xx in through `failure_statuses`."""
    for (method, path), contract in OPERATION_CONTRACTS.items():
        stray = sorted(code for code in contract.failure_statuses if 200 <= code < 300)
        assert stray == [], f"{method} {path}: 2xx codes in failure_statuses: {stray}"


# ── G3 · no broad buckets ────────────────────────────────────────────────────

BROAD_BUCKETS = frozenset({"default", "1XX", "2XX", "3XX", "4XX", "5XX"})


def test_g3_no_broad_status_buckets_anywhere(spec: dict[str, Any]) -> None:
    """`default` / `2XX` / `4XX` / `5XX` are exactly what the ticket rejects."""
    for responses in iter_response_maps(spec):
        offending = sorted(BROAD_BUCKETS.intersection(responses))
        assert offending == [], f"broad status bucket(s) in the served document: {offending}"
        malformed = sorted(code for code in responses if not re.fullmatch(r"[1-5]\d\d", code))
        assert malformed == [], f"non-numeric status key(s): {malformed}"


# ── G4 · declared failures cover the static scan ─────────────────────────────


def test_g4_declared_statuses_cover_static_scan(contract_app: FastAPI) -> None:
    """A new raise site nobody declared turns this red."""
    scanned = scan_app(contract_app)
    undeclared: dict[tuple[str, str], list[int]] = {}
    for key, codes in scanned.items():
        declared = set(OPERATION_CONTRACTS[key].declared_statuses())
        missing = sorted(set(codes) - declared)
        if missing:
            undeclared[key] = missing
    assert undeclared == {}, (
        "status codes reachable in code but absent from OPERATION_CONTRACTS: "
        f"{ {f'{m} {p}': c for (m, p), c in undeclared.items()} }"
    )


# ── G5 · unknown-query declaration is mechanical ─────────────────────────────


def iter_dependants(dependant: Any) -> Iterator[Any]:
    yield dependant
    for sub in dependant.dependencies:
        yield from iter_dependants(sub)


def mechanical_unknown_query(route: APIRoute) -> str:
    """`reject` only if some query parameter is a model that forbids extras.

    Read off ``route.dependant`` (including sub-dependencies), never from the
    registry: this is the whole point of G5. FastAPI ignores undeclared query
    parameters unless a bound Pydantic model sets ``extra="forbid"``.
    """
    for dependant in iter_dependants(route.dependant):
        for field in dependant.query_params:
            annotation = field.field_info.annotation
            if (
                isinstance(annotation, type)
                and issubclass(annotation, BaseModel)
                and annotation.model_config.get("extra") == "forbid"
            ):
                return "reject"
    return "passthrough"


def test_g5_unknown_query_declaration_matches_the_route(
    contract_app: FastAPI, spec: dict[str, Any]
) -> None:
    for method, path, route in served_routes(contract_app):
        contract = OPERATION_CONTRACTS[(method, path)]
        assert contract.unknown_query in {"reject", "passthrough"}
        expected = mechanical_unknown_query(route)
        assert contract.unknown_query == expected, (
            f"{method} {path}: declared unknown_query={contract.unknown_query!r} but the route "
            f"mechanically resolves to {expected!r}"
        )
        served = spec["paths"][path][method.lower()]["x-lending-unknown-query-parameters"]
        assert served == expected


def test_g5_passthrough_routes_really_accept_unknown_query() -> None:
    """Two spot checks that the mechanical verdict matches observed behaviour."""
    app = create_app()
    asyncio.run(create_tables(app.state.engine))
    client = TestClient(app)
    response = client.get(
        "/api/v1/admin/wedap-import/delivery-report?totally_unknown=1",
        headers={"X-Caller-Service": "gate-probe"},
    )
    assert response.status_code == 200, response.text
    assert client.get("/healthz?totally_unknown=1").status_code == 200


# ── G6 · required response headers are proven by real responses ──────────────


def test_g6_required_headers_are_present_on_real_responses(
    probes: dict[tuple[str, str, int], httpx.Response],
) -> None:
    for (method, path, status), response in probes.items():
        contract = OPERATION_CONTRACTS[(method, path)]
        assert response.status_code == status, (
            f"{method} {path}: probe was supposed to produce {status}, got {response.status_code}"
        )
        for header in contract.headers_for(status):
            assert header in response.headers, (
                f"{method} {path} {status}: declared header {header!r} is missing from the "
                "real response"
            )
            assert response.headers[header].strip() != ""


def test_g6_every_status_specific_header_declaration_has_evidence(
    probes: dict[tuple[str, str, int], httpx.Response],
) -> None:
    """Fail-closed: declaring a header for a status nobody probes is not allowed."""
    required = {
        (method, path, status)
        for (method, path), contract in OPERATION_CONTRACTS.items()
        for status in contract.required_headers
    }
    assert required - set(probes) == set(), (
        "status-specific header declarations with no real-response evidence -- add a probe "
        "to the `probes` fixture or drop the declaration"
    )


def test_g6_authentication_challenge_matches_the_mounted_middleware(
    probes: dict[tuple[str, str, int], httpx.Response],
) -> None:
    """The 401 surface is derived from the middleware, and the value really matches."""
    app = create_app(Settings(wedap_callback_api_key=PROBE_CALLBACK_KEY))
    for (method, path), contract in OPERATION_CONTRACTS.items():
        challenge = authentication_challenge(app, path)
        if 401 in contract.failure_statuses:
            assert challenge is not None, f"{method} {path} declares 401 but is an exempt path"
            assert probes[(method, path, 401)].headers["WWW-Authenticate"] == challenge
        else:
            assert challenge is None, (
                f"{method} {path} omits 401 but the mounted middleware would challenge it"
            )


def test_g6_universal_headers_hold_on_every_probed_response(
    probes: dict[tuple[str, str, int], httpx.Response],
) -> None:
    for (method, path, status), response in probes.items():
        for header in UNIVERSAL_RESPONSE_HEADERS:
            assert header in response.headers, f"{method} {path} {status}: missing {header}"
    assert probes[("GET", "/api/v1/deposit/accounts", 500)].headers["Cache-Control"] == "no-store"


# ── G7 · error media type is the one really served ───────────────────────────


def test_g7_error_media_type_matches_reality(
    probes: dict[tuple[str, str, int], httpx.Response], spec: dict[str, Any]
) -> None:
    probed_operations: set[tuple[str, str]] = set()
    for (method, path, status), response in probes.items():
        contract = OPERATION_CONTRACTS[(method, path)]
        served = response.headers["content-type"].split(";")[0].strip()
        assert served == contract.error_media_type, (
            f"{method} {path} {status}: declared {contract.error_media_type!r}, served {served!r}"
        )
        declared_media = spec["paths"][path][method.lower()]["responses"][str(status)]["content"]
        assert list(declared_media) == [contract.error_media_type]
        probed_operations.add((method, path))
    assert probed_operations == set(OPERATION_CONTRACTS), (
        "every operation needs at least one real error response behind its media-type claim"
    )


def test_g7_compliance_flags_are_declared_not_narrated(spec: dict[str, Any]) -> None:
    """The Problem-envelope gap is machine-readable, and not silently 'fixed'."""
    for path, item in spec["paths"].items():
        for method, operation in item.items():
            contract = OPERATION_CONTRACTS[(method.upper(), path)]
            assert contract.problem_compliant is False
            assert contract.error_media_type == "application/json"
            assert operation["x-lending-problem-compliance"] is False
            assert operation["x-lending-exception-ref"] == PROBLEM_COMPLIANCE_EXCEPTION_REF


# ── G8 · ordering: auth beats validation, budget beats auth ──────────────────


def test_g8_authentication_precedes_body_and_query_validation() -> None:
    """Bad credentials + unknown query + bad body must answer 401, not 422/404."""
    client = TestClient(create_app())
    response = client.post(
        "/api/v1/bank-funds/collect-from-users?unknown_param=1",
        json={"not": "a valid body"},
    )
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("S2S ")
    assert response.json()["error"]["code"] == "GW_401_S2S"


def test_g8_authentication_precedes_validation_with_a_bad_token() -> None:
    """Same ordering when a shared secret is configured and the token is wrong."""
    app = create_app(Settings(s2s_secret="gate-shared-secret"))  # noqa: S106 - fixed test value
    response = TestClient(app).post(
        "/api/v1/bank-funds/collect-from-users?unknown_param=1",
        json={"not": "a valid body"},
        headers={"X-Caller-Service": "gate-probe", "X-S2S-Token": "wrong"},
    )
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("S2S ")


def test_g8_request_target_budget_precedes_authentication() -> None:
    """§7.2.1 step 1: the 8,192-byte budget is settled before the 401 is decided."""
    response = TestClient(create_app()).post(
        f"/api/v1/bank-funds/collect-from-users?{OVERLONG_QUERY}"
    )
    assert response.status_code == 414
    assert response.json()["error"]["code"] == "GW_414_URI_TOO_LONG"


def test_g8_path_not_found_still_authenticates_first() -> None:
    """An unrouted target under an authenticated prefix answers 401, not 404."""
    response = TestClient(create_app()).get("/api/v1/definitely-not-a-route")
    assert response.status_code == 401


# ── G9 · fault injection lands on a declared status with zero side effects ───


@pytest.mark.parametrize(
    ("side_effect", "expected_status"),
    [
        (httpx.TimeoutException("upstream timed out"), 502),
        (
            httpx.HTTPStatusError(
                "gateway timeout",
                request=httpx.Request("GET", "http://wedap/deposit/accounts"),
                response=httpx.Response(504),
            ),
            502,
        ),
        (WedapError("E999", "upstream rejected the request"), 502),
        (ValueError("unmapped failure"), 500),
    ],
    ids=["timeout", "upstream-504", "wedap-business-error", "unmapped-exception"],
)
def test_g9_injected_upstream_fault_is_declared_and_has_no_side_effect(
    fault_client: tuple[TestClient, AsyncMock, Any],
    side_effect: Exception,
    expected_status: int,
) -> None:
    client, wedap, session_factory = fault_client
    before = asyncio.run(row_counts(session_factory))
    wedap.get_deposit_accounts.side_effect = side_effect

    response = client.get("/api/v1/deposit/accounts", headers=FAULT_HEADERS)

    contract = OPERATION_CONTRACTS[("GET", "/api/v1/deposit/accounts")]
    assert response.status_code == expected_status
    assert response.status_code in contract.declared_statuses(), (
        f"fault produced {response.status_code}, which the registry does not declare"
    )
    for header in contract.headers_for(response.status_code):
        assert header in response.headers
    assert response.headers["content-type"].split(";")[0] == contract.error_media_type

    after = asyncio.run(row_counts(session_factory))
    assert after == before, f"the failed request wrote to the database: {before} -> {after}"
    assert sum(after.values()) == 0
    assert wedap.get_deposit_accounts.await_count == 1
    assert len(wedap.mock_calls) == 1, (
        f"the failed request made extra outbound calls: {wedap.mock_calls}"
    )


def test_g9_readyz_probe_failure_is_declared_and_has_no_side_effect() -> None:
    """503 with its Retry-After, and not a single row written."""
    app = create_app()
    asyncio.run(create_tables(app.state.engine))
    real_factory = app.state.session_factory
    before = asyncio.run(row_counts(real_factory))

    def exploding_factory() -> Any:
        raise RuntimeError("injected: database is unreachable")

    app.state.session_factory = exploding_factory
    response = TestClient(app).get("/readyz")

    contract = OPERATION_CONTRACTS[("GET", "/readyz")]
    assert response.status_code == 503
    assert 503 in contract.declared_statuses()
    for header in contract.headers_for(503):
        assert header in response.headers
    assert response.headers["Retry-After"].strip() != ""

    app.state.session_factory = real_factory
    assert asyncio.run(row_counts(real_factory)) == before
    assert sum(before.values()) == 0


def test_g9_successful_call_does_write_so_the_zero_assertion_can_fail(
    fault_client: tuple[TestClient, AsyncMock, Any],
) -> None:
    """Control case: the same endpoint on the happy path writes exactly one audit row.

    Without this, "zero rows after a fault" would also hold if the endpoint never
    wrote anything under any circumstance -- i.e. the side-effect assertion above
    would be incapable of failing.
    """
    client, wedap, session_factory = fault_client
    wedap.get_deposit_accounts.side_effect = None
    wedap.get_deposit_accounts.return_value = {"accounts": []}

    response = client.get("/api/v1/deposit/accounts", headers=FAULT_HEADERS)

    assert response.status_code == 200
    assert asyncio.run(row_counts(session_factory))["query_audit"] == 1


# ── G10 · structural validity and digest ─────────────────────────────────────


def test_g10_document_is_structurally_valid_openapi_31(spec: dict[str, Any]) -> None:
    assert spec["openapi"].startswith("3.1"), spec["openapi"]
    assert spec["info"]["title"] and spec["info"]["version"]
    assert spec["paths"], "an empty paths object is not a contract"

    schemas = spec["components"]["schemas"]
    for ref in iter_refs(spec):
        assert ref.startswith("#/components/schemas/"), f"unsupported $ref target: {ref}"
        assert ref.rsplit("/", 1)[1] in schemas, f"dangling $ref: {ref}"

    for path, item in spec["paths"].items():
        assert path.startswith("/")
        for method, operation in item.items():
            assert method in {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
            responses = operation["responses"]
            assert responses, f"{method.upper()} {path}: empty responses object"
            for status, response in responses.items():
                where = f"{method.upper()} {path} {status}"
                assert response.get("description"), f"{where}: OpenAPI requires a description"
                assert response.get("content"), f"{where}: no content declared"
                for media_type, media in response["content"].items():
                    assert "/" in media_type, f"{where}: bad media type {media_type!r}"
                    assert media.get("schema"), f"{where}: media type {media_type} has no schema"
                for name, header in response.get("headers", {}).items():
                    assert header["required"] is True, f"{where}: header {name} is not required"
                    assert header["schema"] == {"type": "string"}
                    assert header.get("description"), f"{where}: header {name} has no description"


def test_g10_checked_in_document_and_digest_match_the_live_spec(spec: dict[str, Any]) -> None:
    """The reviewed artefact under docs/api-contract is the served document."""
    assert OPENAPI_JSON.exists(), "missing docs/api-contract/openapi.json -- run the generator"
    assert OPENAPI_SHA256.exists(), "missing docs/api-contract/openapi.sha256"

    live = canonical_bytes(spec)
    assert OPENAPI_JSON.read_bytes() == live, (
        "docs/api-contract/openapi.json is stale -- regenerate it and review the diff"
    )
    recorded_text = OPENAPI_SHA256.read_text(encoding="utf-8")
    recorded = recorded_text.split()[0]
    assert recorded == hashlib.sha256(live).hexdigest(), (
        "docs/api-contract/openapi.sha256 does not match the document"
    )
    # The digest line alone does not say *what* was hashed, and the four repos in this
    # batch disagree (two hash the file bytes, two hash a canonical re-serialization).
    # A consumer who guesses wrong concludes the artefact was tampered with. The
    # self-description was hand-added once and silently wiped by the next generator run
    # (2026-08-31), which is why it is asserted here rather than left to discipline.
    assert "sha256sum -c openapi.sha256" in recorded_text, (
        "openapi.sha256 lost its self-description -- regenerate with "
        "scripts/gen_openapi_contract.py, which writes it"
    )


def _render_operations_row(method: str, path: str, contract: OperationContract) -> tuple[str, ...]:
    """The nine cells ``operations.md`` must carry for one operation, from the registry."""
    failures = ", ".join(f"`{status}`" for status in contract.failure_statuses)
    specific = "; ".join(
        f"{status}: " + ", ".join(f"`{header}`" for header in contract.required_headers[status])
        for status in sorted(contract.required_headers)
    )
    return (
        f"`{method}`",
        f"`{path}`",
        f"`{contract.success_status}`",
        failures,
        f"`{contract.profile}`",
        f"`{contract.unknown_query}`",
        f"`{contract.error_media_type}`",
        "yes" if contract.problem_compliant else "no",
        specific or "\u2014",
    )


def _parse_operations_table() -> dict[tuple[str, str], tuple[str, ...]]:
    """Every nine-column data row of the operation table, keyed by (method, path).

    The file also holds an authentication-surface table and a gate table; those are
    skipped by requiring nine cells whose first is a back-ticked HTTP method.
    """
    methods = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
    rows: dict[tuple[str, str], tuple[str, ...]] = {}
    for line in (DOCS_DIR / "operations.md").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            continue
        cells = tuple(cell.strip() for cell in stripped.strip("|").split("|"))
        if len(cells) != 9 or cells[0].strip("`") not in methods:
            continue
        key = (cells[0].strip("`"), cells[1].strip("`"))
        assert key not in rows, f"operations.md lists {key} twice"
        rows[key] = cells
    return rows


def test_g10_operations_table_matches_the_registry_column_for_column() -> None:
    """The table handed to external teams must not be able to lie.

    It is hand-maintained prose around machine-checked rows: asserting only that a
    row exists would let every other column (failure codes, profile, unknown_query,
    media type, compliance flag, headers) drift silently while this gate stayed
    green -- and a stale contract table is worse than none, because a consumer
    writes parsing code against it.
    """
    actual = _parse_operations_table()
    expected = {
        (method, path): _render_operations_row(method, path, contract)
        for (method, path), contract in OPERATION_CONTRACTS.items()
    }
    assert actual.keys() == expected.keys(), (
        "operations.md rows and the registry disagree on which operations exist: "
        f"only in the table {sorted(actual.keys() - expected.keys())}, "
        f"only in the registry {sorted(expected.keys() - actual.keys())}"
    )
    for key in sorted(expected):
        assert actual[key] == expected[key], (
            f"operations.md row for {key[0]} {key[1]} has drifted from the registry:\n"
            f"  table:    {actual[key]}\n  registry: {expected[key]}"
        )


def test_g10_registry_entries_are_frozen_dataclasses() -> None:
    """A snapshot that can be edited at runtime is not a snapshot."""
    contract = next(iter(OPERATION_CONTRACTS.values()))
    assert isinstance(contract, OperationContract)
    with pytest.raises(FrozenInstanceError):
        contract.success_status = 201  # type: ignore[misc]


# ── G11 · every operation isolates the query location, new routes included ───


def test_g11_every_operation_rejects_a_duplicated_query_parameter() -> None:
    """API-HTTP-022 on the served surface, one real request per operation.

    Written as a loop over ``served_routes`` rather than a list of paths on purpose:
    the guard is mounted once, at application level, and this gate is what makes that
    mounting provable. A route added tomorrow is probed by this test the day it is
    added -- if somebody ever replaces the app-level dependency with per-router
    mounting and forgets one router, the new operation answers 200 here and the gate
    goes red. It reads only the response, never the mounting, so it stays correct
    across FastAPI versions that represent ``include_router`` differently.
    """
    app = create_app()
    asyncio.run(create_tables(app.state.engine))
    client = TestClient(app, raise_server_exceptions=False)

    for method, path, _route in served_routes(app):
        response = client.request(
            method,
            f"{concrete(path)}?dup=1&dup=2",
            headers={"X-Caller-Service": "gate-probe"},
        )
        assert response.status_code == 422, (
            f"{method} {path}: duplicated query parameter answered "
            f"{response.status_code}, not 422 -- is the query-location guard mounted?"
        )
        assert response.json()["error"]["code"] == "GW_422_DUPLICATE_QUERY_PARAMETER"
        assert 422 in OPERATION_CONTRACTS[(method, path)].declared_statuses(), (
            f"{method} {path}: the guard's 422 is reachable but undeclared"
        )


def test_g11_every_operation_rejects_a_header_named_query_parameter() -> None:
    """The other half of the location rule: a header-located name sent as query."""
    app = create_app()
    asyncio.run(create_tables(app.state.engine))
    client = TestClient(app, raise_server_exceptions=False)

    for method, path, _route in served_routes(app):
        response = client.request(
            method,
            f"{concrete(path)}?X-Tenant-Id=smuggled",
            headers={"X-Caller-Service": "gate-probe"},
        )
        assert response.status_code == 422, f"{method} {path}: {response.status_code}"
        assert response.json()["error"]["code"] == "GW_422_QUERY_LOCATION_CONFLICT"


def test_g11_control_a_well_formed_query_is_not_rejected() -> None:
    """Without this, G11 would also pass on a service that 422s every request."""
    app = create_app()
    asyncio.run(create_tables(app.state.engine))
    client = TestClient(app, raise_server_exceptions=False)

    guard_codes = {"GW_422_DUPLICATE_QUERY_PARAMETER", "GW_422_QUERY_LOCATION_CONFLICT"}
    rejected: list[tuple[str, str, str]] = []
    for method, path, _route in served_routes(app):
        response = client.request(
            method,
            f"{concrete(path)}?single=1",
            headers={"X-Caller-Service": "gate-probe"},
        )
        # A 422 is allowed here -- `GET /api/v1/admin/platform-accounts` has a
        # required `tenantId` query and legitimately rejects a request without it.
        # What must never happen is the *guard* firing on a single well-formed name.
        code = (response.json().get("error") or {}).get("code")
        if code in guard_codes:
            rejected.append((method, path, code))
    assert rejected == [], (
        "the query-location guard rejected a single, well-formed, non-repeated query "
        f"parameter: {rejected}"
    )


def test_g11_header_location_names_are_the_ones_this_service_really_reads() -> None:
    """The reserved list is a claim about the code; check it against the code.

    A header this service reads that is *missing* from the list is a smuggling route
    left open. Grepping the application package for the literal is how the list was
    built in the first place, so this is the check that keeps it from rotting when a
    new header is introduced.
    """
    from app.core.query_location import HEADER_LOCATION_NAMES

    app_dir = Path(__file__).resolve().parent.parent / "app"
    sources = "\n".join(path.read_text(encoding="utf-8") for path in sorted(app_dir.rglob("*.py")))
    header_reads = set(re.findall(r'request\.headers\.get\(\s*"([^"]+)"', sources))

    missing = sorted(name for name in header_reads if name.lower() not in HEADER_LOCATION_NAMES)
    assert missing == [], (
        f"header fields read by this service but absent from HEADER_LOCATION_NAMES: {missing}"
    )


def test_g11_no_handler_reads_the_raw_query_multidict() -> None:
    """Handlers consume the validated mapping, never ``request.query_params``.

    Defence in depth rather than a duplicate of the behavioural gates above: with the
    boundary guard mounted, ``dict(request.query_params)`` happens to be equivalent
    today, because the only inputs on which the two differ are exactly the ones the
    guard rejects. That equivalence is a property of the current guard, not of the
    handler -- narrow the guard tomorrow and a handler still holding the raw
    multidict silently goes back to last-wins. Keeping the last-wins primitive out of
    handler code is what makes that impossible rather than merely unlikely.
    """
    api_dir = Path(__file__).resolve().parent.parent / "app" / "api"
    offenders = [
        f"{path.relative_to(api_dir.parent.parent)}:{number}"
        for path in sorted(api_dir.rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "request.query_params" in line
    ]
    assert offenders == [], (
        "handlers must call app.core.query_location.query_location_params instead of "
        f"reading the raw query multidict: {offenders}"
    )


# ---------------------------------------------------------------------------
# G12 --- "you will get 401" is only half a contract
# ---------------------------------------------------------------------------
#
# Declaring 401 without declaring ``security`` tells a consumer "this will reject
# you" and nothing about how not to be rejected. An SDK generated from such a spec
# has no credential parameter on those calls at all, so the caller finds out at
# runtime by getting a 401.
#
# Both sides are derived from the same mounted ``S2SMiddleware`` instance that
# ``authentication_challenge`` reads: the challenge decides whether an operation
# can answer 401, and the same challenge decides which credential avoids it.
# Deriving them apart is how they drift; deriving them together makes any drift a
# contradiction these gates can see.
#
# Sister gates: lending-core's ``test_openapi_security_declared.py``, baffle's
# equivalent and lending-recon's G11. Measured 2026-08-31, this service declared
# 401 on 24 operations and a credential on none of them.


def test_g12_security_set_equals_the_401_set(spec: dict[str, Any]) -> None:
    with_security = {
        (method, path)
        for path, item in spec["paths"].items()
        for method, operation in item.items()
        if method in {"get", "post", "put", "patch", "delete"} and operation.get("security")
    }
    with_401 = {
        (method, path)
        for path, item in spec["paths"].items()
        for method, operation in item.items()
        if method in {"get", "post", "put", "patch", "delete"}
        and "401" in (operation.get("responses") or {})
    }
    assert with_security == with_401, (
        "The 'needs a credential' set and the 'answers 401' set disagree. Both come from "
        "the mounted S2SMiddleware, so a disagreement means one derivation is wrong -- and "
        "neither is checkable by looking at it alone.\n"
        f"  declares 401 but no credential: {sorted(with_401 - with_security)[:5]}\n"
        f"  declares a credential but no 401: {sorted(with_security - with_401)[:5]}"
    )


def test_g12_the_set_is_not_empty(spec: dict[str, Any]) -> None:
    """Anti-vacuity guard: two empty sets are also equal."""
    count = sum(
        1
        for _path, item in spec["paths"].items()
        for method, operation in item.items()
        if method in {"get", "post", "put", "patch", "delete"} and operation.get("security")
    )
    assert count >= 20, (
        f"Only {count} operations carry security. All but four of this service's operations "
        "sit behind S2SMiddleware, so a small number means the route walk lost them rather "
        "than that the surface is public."
    )


def test_g12_each_operation_declares_the_credential_its_challenge_names(
    contract_app: FastAPI, spec: dict[str, Any]
) -> None:
    """The declared scheme must match the challenge that path really answers with.

    An S2S path that advertised the callback's ``apikey`` (or the reverse) would
    send integrators to collect the wrong header -- a failure the set comparison
    above cannot see, because both sets would still be the same size.
    """
    expected_by_challenge = {
        CHALLENGE_S2S: {"S2SToken", "CallerService"},
        CHALLENGE_APIKEY: {"BankApiKey"},
    }
    for method, path, _route in served_routes(contract_app):
        operation = spec["paths"][path][method.lower()]
        challenge = authentication_challenge(contract_app, path)
        declared = {name for requirement in operation.get("security") or [] for name in requirement}
        if challenge is None:
            assert not declared, f"{method} {path} is exempt from S2S but declares {declared}"
        else:
            assert declared == expected_by_challenge[challenge], (
                f"{method} {path} answers 401 with {challenge!r} but declares {declared or 'nothing'}"
            )


def test_g12_every_referenced_scheme_is_defined(spec: dict[str, Any]) -> None:
    defined = set((spec.get("components") or {}).get("securitySchemes") or {})
    for path, item in spec["paths"].items():
        for method, operation in item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            for requirement in operation.get("security") or []:
                for name in requirement:
                    assert name in defined, f"{method.upper()} {path} references undefined scheme {name!r}"


def test_g12_scheme_definitions_carry_no_credential_material(spec: dict[str, Any]) -> None:
    """A securityScheme declares the shape of a credential, never a credential."""
    blob = json.dumps((spec.get("components") or {}).get("securitySchemes") or {}, ensure_ascii=False)
    for pattern, what in [
        (r"eyJ[A-Za-z0-9_-]{10,}", "a JWT literal"),
        (r"BEGIN [A-Z ]*PRIVATE KEY", "a private key"),
        (r"(?i)\b(secret|token|apikey)\s*[:=]\s*\S{8,}", "a credential assignment"),
    ]:
        assert not re.search(pattern, blob), f"securitySchemes contains {what}"
