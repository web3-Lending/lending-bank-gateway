"""Per-operation OpenAPI declaration snapshot plus the generator that emits it.

Why this module exists
----------------------
FastAPI's auto-generated schema declares ``200`` and ``422`` for every operation and
nothing else. That is simultaneously too little (a caller cannot see that
``/api/v1/deposit/accounts`` answers ``502`` when wedap is down, or that every
non-exempt path answers ``401`` with a ``WWW-Authenticate`` challenge) and wrong
(``422`` is declared on operations that have no validatable parameter, and its body
schema is FastAPI's ``HTTPValidationError``, which this service never returns -- the
custom ``_validation_exception_handler`` returns the ``err()`` envelope instead).

:data:`OPERATION_CONTRACTS` is the hand-curated, checked-in answer: for each of the
28 operations, the single success status, the failure statuses that are genuinely
reachable **today**, the media type the error envelope is **actually** served with,
and the response headers this service **actually** sends.

Truthfulness rules this snapshot obeys
--------------------------------------
* Only statuses this build can really return. ``429`` is absent because no rate
  limiter exists in this repository (a whole-repo grep for ``429`` hits three
  comments and no code). ``405`` is absent because, although Starlette really does
  answer ``405`` with an ``Allow`` header, it does so only for a *different* method
  on the same path -- never for the ``(method, path)`` pair the operation *is*.
  ``404`` for an unrouted target and ``307`` for trailing-slash redirects likewise
  belong to other request-targets, not to any operation here; the four ``404``s that
  are declared are raised by handlers themselves.
* ``error_media_type`` is ``application/json`` everywhere because that is what the
  handlers return **now**. ``application/problem+json`` and the ``LendingProblemV1``
  schema are deliberately **not** declared: the Problem-envelope migration is a
  separate, breaking batch. Declaring a schema the service does not serve is worse
  than declaring nothing -- consumers write parsers against it and break.
* Consequently ``problem_compliant`` is ``False`` for all 28 operations, and every
  operation carries ``x-lending-exception-ref`` pointing at the follow-up that
  tracks the gap. The non-compliance is thereby machine-queryable rather than
  buried in prose.

How the failure sets were derived
---------------------------------
Global base set --- ``{414, 500}``:
    ``414`` from :class:`~app.core.context.RequestTargetLimitMiddleware`, which is
    mounted outside the auth middleware and therefore fires on exempt paths too.
    ``500`` from ``add_exception_handler(Exception, ...)``: this service registers
    **no** domain-exception-to-status mapping, so every ``WedapError`` /
    ``IdempotencyConflict`` / ``ValueError`` that escapes a handler's own
    ``try/except`` lands on the catch-all.

``401`` --- read off the live middleware stack, not hardcoded:
    :class:`~app.core.s2s.S2SMiddleware` is the only place authentication happens
    (no route in this repository has an auth dependency in its ``dependant``). Its
    ``exempt_paths`` kwarg carries the four operations that have no ``401``; its
    ``callback_paths`` kwarg carries the two that answer ``401`` with the
    ``ApiKey`` challenge; the remaining 22 answer with the ``S2S`` challenge. See
    :func:`authentication_challenge`, which derives this from the mounted
    middleware so the snapshot cannot drift from configuration.

``400`` --- ``app.api.deps.require_headers`` (missing ``X-Tenant-Id`` /
    ``X-Request-Id``), reachable on 18 operations: 15 mount it as ``Depends`` and
    three call it imperatively inside the handler body (``callbacks.py``,
    ``recon_notify.py``, ``wedap_import_enqueue.py``) -- the imperative three are
    invisible to a ``route.dependant`` walk, which is exactly why the static scanner,
    not the dependant, is the authority here. Two further operations
    (``POST``/``PATCH`` on ``/admin/platform-accounts``) raise their own ``400``.

``403`` --- six operations: the three ``/admin/platform-accounts`` endpoints
    (``_require_admin_caller``) and the three ``bank-funds`` write primitives that
    route through ``bank_funds._submit`` ->
    :func:`~app.services.account_guard.assert_platform_account_allowed`.
    ``/bank-funds/reversals`` and both ``/loans`` write primitives do **not** pass
    through the guard (``loans._submit`` is a separate helper and ``submit_reversal``
    is called directly), so they do not declare ``403``.

``422`` --- only where a parameter can actually fail validation. See
    :data:`NO_422_JUSTIFICATION` for the two operations whose ``route.dependant``
    is non-empty and yet cannot produce a ``422``; both were probed against a live
    ``TestClient`` before being excluded.

``502`` --- the seven wedap passthrough queries in ``app/api/v1/deposit.py``, which
    map every upstream failure onto ``502 GW_502_UPSTREAM``.

``503`` --- ``/readyz`` only, and it is the sole status in this service that carries
    a ``Retry-After`` header.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, cast

from fastapi import FastAPI

#: Follow-up that tracks the Problem-envelope (RFC 9457) migration. Emitted as
#: ``x-lending-exception-ref`` on every operation, so a consumer reading the spec
#: can see *why* the error bodies are not Problem documents yet.
PROBLEM_COMPLIANCE_EXCEPTION_REF = "FU-API-V22-HUB-BLOCKERS-20260828-001"

#: Response headers this service attaches to **every** response, on every status.
#: ``IdentifierMiddleware`` sets all three on the way out; the catch-all 500 handler
#: re-applies them itself because it runs outside that middleware. They are emitted
#: on every declared response by :func:`build_openapi` rather than being repeated in
#: all 28 registry entries -- :attr:`OperationContract.required_headers` carries only
#: the status-specific overlay.
UNIVERSAL_RESPONSE_HEADERS: tuple[str, ...] = (
    "Cache-Control",
    "X-Request-Id",
    "X-Trace-Id",
)

#: ``WWW-Authenticate`` challenge values, verbatim from ``app/core/s2s.py``.
CHALLENGE_S2S = 'S2S realm="lending-bank-gateway", header="X-S2S-Token"'
CHALLENGE_APIKEY = 'ApiKey realm="lending-bank-gateway", header="apikey"'

#: Statuses whose ``WWW-Authenticate`` requirement is universal on authenticated
#: operations (API-HTTP-004): every 401 in this service leaves via ``_unauthorized``.
_AUTH_HEADERS: Mapping[int, tuple[str, ...]] = {401: ("WWW-Authenticate",)}

#: Failure statuses every operation can produce -- see the module docstring.
GLOBAL_BASE_FAILURES: tuple[int, ...] = (414, 500)

#: Operations whose ``route.dependant`` carries a parameter and that nonetheless
#: cannot emit a ``422``, with the reason. Recorded here rather than silently
#: omitted, because a mechanical "has parameters => 422 is reachable" rule would
#: include them and a reader deserves to see why this snapshot disagrees.
NO_422_JUSTIFICATION: Mapping[tuple[str, str], str] = {
    ("GET", "/api/v1/admin/wedap-import/delivery-report"): (
        "the only parameter is `import_date: str | None = None` -- an unconstrained "
        "optional string query. Every input is a valid `str`, and a repeated query key "
        "binds the last value rather than failing. Probed: "
        "`?import_date=a&import_date=b` reaches the handler."
    ),
    ("GET", "/api/v1/loans/p2p-repayments/{biz_seq_no}/status"): (
        "the only parameter is an unconstrained `str` path segment. A segment that "
        "fails to bind cannot exist -- an empty segment does not match the route at "
        "all (404 on a different request-target). Probed with a whitespace segment: "
        "reaches the handler."
    ),
}


@dataclass(frozen=True)
class OperationContract:
    """What one operation really returns, today.

    Attributes:
        success_status: The single 2xx this operation returns. Exactly one, by
            construction -- no route in this repository has more than one success
            path with a distinct status.
        failure_statuses: Every non-2xx this operation can really return, sorted.
        profile: The v2.2 profile group this operation belongs to. Descriptive;
            it drives no generation logic, it exists so the operations table can
            be read by group.
        unknown_query: ``"reject"`` if the operation rejects query parameters it
            does not declare, ``"passthrough"`` otherwise. Mechanically checkable
            against the route's dependencies -- do not set it aspirationally.
        error_media_type: The media type failure bodies are served with **now**.
        problem_compliant: Whether failure bodies are RFC 9457 Problem documents.
            ``False`` throughout this build.
        required_headers: Status-specific headers this service always sends with
            that status, on top of :data:`UNIVERSAL_RESPONSE_HEADERS`.
    """

    success_status: int
    failure_statuses: tuple[int, ...]
    profile: str
    unknown_query: str
    error_media_type: str
    problem_compliant: bool
    required_headers: Mapping[int, tuple[str, ...]]

    def headers_for(self, status: int) -> tuple[str, ...]:
        """Every header guaranteed present on ``status`` for this operation."""
        specific = self.required_headers.get(status, ())
        return tuple(sorted({*UNIVERSAL_RESPONSE_HEADERS, *specific}))

    def declared_statuses(self) -> tuple[int, ...]:
        """Success plus failures, sorted -- the exact key set of ``responses``."""
        return tuple(sorted({self.success_status, *self.failure_statuses}))


def _contract(
    *,
    failures: tuple[int, ...],
    profile: str,
    authenticated: bool = True,
    extra_headers: Mapping[int, tuple[str, ...]] | None = None,
) -> OperationContract:
    """Build one entry. ``failures`` is the operation's own set; the global base
    (``414``/``500``) and, for authenticated operations, ``401`` are folded in here
    so the 28 literals below stay readable and cannot drift from each other.
    """
    statuses = {*failures, *GLOBAL_BASE_FAILURES}
    headers: dict[int, tuple[str, ...]] = {}
    if authenticated:
        statuses.add(401)
        headers.update(_AUTH_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    return OperationContract(
        success_status=200,
        failure_statuses=tuple(sorted(statuses)),
        profile=profile,
        # No route in this repository mounts a dependency that rejects undeclared
        # query parameters, and the seven deposit passthroughs forward
        # `dict(request.query_params)` upstream verbatim. Declaring "reject" here
        # would be a lie the G5 gate catches.
        unknown_query="passthrough",
        error_media_type="application/json",
        problem_compliant=False,
        required_headers=headers,
    )


#: The whole snapshot. 28 entries, one per operation, keyed ``(METHOD, path)`` with
#: the path in FastAPI's ``path_format`` (templated) form.
OPERATION_CONTRACTS: dict[tuple[str, str], OperationContract] = {
    # ── Unauthenticated probes (S2SMiddleware.exempt_paths) ────────────────────
    # No 401 (exempt), no 400 (no require_headers), no 422 (no parameters at all).
    # 414 still applies: RequestTargetLimitMiddleware sits outside S2S and fires on
    # exempt paths too (probed: `GET /healthz?<9000 bytes>` -> 414 with no-store).
    ("GET", "/healthz"): _contract(failures=(), profile="PUBLIC_PROBE", authenticated=False),
    ("GET", "/readyz"): _contract(
        # 503 twice over: session_factory unwired, and the `SELECT 1` probe failing.
        # Both carry Retry-After -- the only status in this service that does.
        failures=(503,),
        profile="PUBLIC_PROBE",
        authenticated=False,
        extra_headers={503: ("Retry-After",)},
    ),
    ("GET", "/build-info"): _contract(failures=(), profile="PUBLIC_PROBE", authenticated=False),
    ("GET", "/api/version"): _contract(failures=(), profile="PUBLIC_PROBE", authenticated=False),
    # ── Inbound wedap callbacks (apikey challenge) ─────────────────────────────
    # 401 is configuration-gated: an empty GW_WEDAP_CALLBACK_API_KEY degrades to
    # pass-through, but `create_app` fail-fasts on that outside local/test, so every
    # deployed environment can and does return it. 400 comes from the imperative
    # `require_headers(request)` call in the handler body plus the body checks.
    # 422 from the `body: dict[str, Any]` parameter (non-object / unparseable JSON).
    ("POST", "/api/v1/callbacks/wedap/transactions"): _contract(
        failures=(400, 422), profile="INBOUND_CALLBACK"
    ),
    ("POST", "/api/v1/recon/notify"): _contract(failures=(400, 422), profile="INBOUND_CALLBACK"),
    # ── Admin ops ─────────────────────────────────────────────────────────────
    ("POST", "/api/v1/admin/outbox/{outbox_id}/replay"): _contract(
        # 404 GW_404_OUTBOX (row absent or not DEAD); 422 from the `int` path
        # parameter (probed: `/api/v1/admin/outbox/abc/replay` -> 422).
        # No 400: this handler does not go through require_headers.
        failures=(404, 422),
        profile="ADMIN_OPS",
    ),
    ("GET", "/api/v1/admin/stuck-orders"): _contract(
        # No parameters, no require_headers, no handler-raised status.
        failures=(),
        profile="ADMIN_OPS",
    ),
    ("GET", "/api/v1/admin/wedap-import/delivery-report"): _contract(
        # 422 deliberately absent -- see NO_422_JUSTIFICATION.
        failures=(),
        profile="ADMIN_OPS",
    ),
    # ── Admin configuration surface (platform-account allow-list) ─────────────
    # 403 GW_403_ADMIN_CALLER is unconditional here (`_require_admin_caller`
    # fail-closes when GW_ADMIN_CALLERS is empty), unlike the account-guard 403 below.
    ("GET", "/api/v1/admin/platform-accounts"): _contract(
        # 422 from the required `tenantId` query (Query(min_length=1)).
        failures=(403, 422),
        profile="ADMIN_CONFIG",
    ),
    ("POST", "/api/v1/admin/platform-accounts"): _contract(
        # 400 invalid status / invalid allowedScopes csv; 409 GW_409_DUPLICATE on the
        # unique-key IntegrityError; 422 from the request-body model.
        failures=(400, 403, 409, 422),
        profile="ADMIN_CONFIG",
    ),
    ("PATCH", "/api/v1/admin/platform-accounts/{row_id}"): _contract(
        # 400 explicit-null on a required field / bad status / bad scopes csv;
        # 404 GW_404_PLATFORM_ACCOUNT; 422 from the body model and the `int` path id.
        failures=(400, 403, 404, 422),
        profile="ADMIN_CONFIG",
    ),
    # ── MONEY_WRITE primitives ────────────────────────────────────────────────
    # All six: 400 (require_headers, Idempotency-Key mismatch, amount guards, wedap
    # required/rejected field gates, invalid bizSeqNo, ValueError out of the submit
    # service), 409 GW_409_IDEMPOTENCY, 422 (request-body model).
    # 403 only on the three that route through `bank_funds._submit` ->
    # assert_platform_account_allowed. That 403 is gated on
    # GW_ACCOUNT_GUARD_MODE=enforce (default `off`), so it is reachable by
    # configuration rather than unconditionally -- declared because the deployed
    # enforcement posture is the one consumers must code against, and because
    # under-declaring a real 403 is the failure mode this snapshot exists to prevent.
    ("POST", "/api/v1/bank-funds/collect-from-users"): _contract(
        failures=(400, 403, 409, 422), profile="MONEY_WRITE"
    ),
    ("POST", "/api/v1/bank-funds/distribute-to-users"): _contract(
        failures=(400, 403, 409, 422), profile="MONEY_WRITE"
    ),
    ("POST", "/api/v1/bank-funds/refunds"): _contract(
        # Extra 422 source beyond the body model: GW_422_FULL_REFUND_USE_REVERSAL,
        # raised by the handler when the refund equals the original order amount.
        failures=(400, 403, 409, 422),
        profile="MONEY_WRITE",
    ),
    ("POST", "/api/v1/bank-funds/reversals"): _contract(
        # No 403: `reverse_transaction` calls submit_reversal directly and never
        # touches assert_platform_account_allowed (verified line by line).
        failures=(400, 409, 422),
        profile="MONEY_WRITE",
    ),
    ("POST", "/api/v1/loans/p2p-disbursements"): _contract(
        # No 403 either: loans has its own `_submit` helper without the account guard.
        failures=(400, 409, 422),
        profile="MONEY_WRITE",
    ),
    ("POST", "/api/v1/loans/p2p-repayments"): _contract(
        failures=(400, 409, 422), profile="MONEY_WRITE"
    ),
    # ── Ledger-backed status queries ──────────────────────────────────────────
    ("GET", "/api/v1/bank-funds/status"): _contract(
        # 400 require_headers; 404 GW_404_ORDER; 422 from the required `bizSeqNo`
        # query (probed: omitting it -> 422). No 502: upstream failures are caught
        # and degraded into the body as `wedap.unavailable`, never surfaced.
        failures=(400, 404, 422),
        profile="LEDGER_QUERY",
    ),
    ("GET", "/api/v1/loans/p2p-repayments/{biz_seq_no}/status"): _contract(
        # 422 deliberately absent -- see NO_422_JUSTIFICATION. Same wedap
        # degrade-to-body behaviour as above, hence no 502.
        failures=(400, 404),
        profile="LEDGER_QUERY",
    ),
    # ── wedap passthrough queries (GATEWAY_BFF) ───────────────────────────────
    # Uniform shape: 400 require_headers, 502 GW_502_UPSTREAM for all three upstream
    # failure classes (timeout/transport, non-2xx, wedap business rejection).
    # No 422 -- none of the seven declares a single validated parameter; each forwards
    # `dict(request.query_params)` upstream verbatim, which is also why
    # unknown_query is `passthrough` rather than `reject`.
    ("GET", "/api/v1/deposit/balances/total"): _contract(
        failures=(400, 502), profile="GATEWAY_BFF"
    ),
    ("GET", "/api/v1/deposit/accounts"): _contract(failures=(400, 502), profile="GATEWAY_BFF"),
    ("GET", "/api/v1/deposit/account/detail"): _contract(
        failures=(400, 502), profile="GATEWAY_BFF"
    ),
    ("GET", "/api/v1/deposit/transactions"): _contract(failures=(400, 502), profile="GATEWAY_BFF"),
    ("GET", "/api/v1/deposit/internal-accounts/info"): _contract(
        failures=(400, 502), profile="GATEWAY_BFF"
    ),
    ("GET", "/api/v1/deposit/internal-accounts/transactions"): _contract(
        failures=(400, 502), profile="GATEWAY_BFF"
    ),
    ("GET", "/api/v1/users/info"): _contract(failures=(400, 502), profile="GATEWAY_BFF"),
    # ── recon -> gateway flow-import hand-off ─────────────────────────────────
    ("POST", "/api/v1/wedap/import/enqueue"): _contract(
        # 400 from the imperative require_headers call; 422 from the body model
        # (seven constrained fields). Enqueue is idempotent by (tenant, request_id)
        # and returns the existing task rather than 409.
        failures=(400, 422),
        profile="INTERNAL_WRITE",
    ),
}


# ── Error-body schema ─────────────────────────────────────────────────────────
# Hand-written rather than derived from a Pydantic model because there is no model:
# `app.core.envelope.err` builds the dict literally. This JSON Schema describes that
# literal exactly -- including `details`, which `err` normalises to `{}` and never
# leaves as null. It is emphatically NOT a Problem document; see the module docstring.

_ERROR_ENVELOPE_SCHEMA_NAME = "GatewayErrorEnvelope"
_ERROR_BODY_SCHEMA_NAME = "GatewayError"

_ERROR_SCHEMAS: Mapping[str, dict[str, Any]] = {
    _ERROR_BODY_SCHEMA_NAME: {
        "title": _ERROR_BODY_SCHEMA_NAME,
        "type": "object",
        "required": ["code", "message", "details"],
        "additionalProperties": False,
        "properties": {
            "code": {
                "type": "string",
                "description": (
                    "Stable machine code, e.g. GW_400_VALIDATION, GW_401_S2S, "
                    "GW_403_ACCOUNT_NOT_ALLOWED, GW_404_ORDER, GW_409_IDEMPOTENCY, "
                    "GW_414_URI_TOO_LONG, GW_422_VALIDATION, GW_500_INTERNAL, "
                    "GW_502_UPSTREAM, GW_503_READYZ. Falls back to `GW_<status>` when "
                    "the raise site supplies a plain-string detail."
                ),
            },
            "message": {"type": "string"},
            "details": {
                "type": "object",
                "additionalProperties": True,
                "description": (
                    "Always present, `{}` when empty. Carries the non-code/message keys "
                    "of the raise site's detail dict, the v2.2 §8.2 MONEY_WRITE typed "
                    "fields on write-primitive 4xx, and `errors` on 422."
                ),
            },
        },
    },
    _ERROR_ENVELOPE_SCHEMA_NAME: {
        "title": _ERROR_ENVELOPE_SCHEMA_NAME,
        "description": (
            "Failure envelope produced by `app.core.envelope.err`. Served as "
            "application/json; this build does NOT emit RFC 9457 Problem documents "
            f"(see x-lending-problem-compliance and {PROBLEM_COMPLIANCE_EXCEPTION_REF})."
        ),
        "type": "object",
        "required": ["success", "data", "error", "trace_id"],
        "additionalProperties": False,
        "properties": {
            "success": {"type": "boolean", "const": False},
            "data": {"type": "null"},
            "error": {"$ref": f"#/components/schemas/{_ERROR_BODY_SCHEMA_NAME}"},
            "trace_id": {"type": "string"},
        },
    },
}

#: Human-readable reason phrases for the declared statuses. OpenAPI requires a
#: `description` on every response object.
_STATUS_DESCRIPTIONS: Mapping[int, str] = {
    200: "Success",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    409: "Conflict",
    414: "URI Too Long",
    422: "Unprocessable Content",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
}

#: Per-header descriptions, so the generated spec explains what each guaranteed
#: header is for instead of listing bare names.
_HEADER_DESCRIPTIONS: Mapping[str, str] = {
    "Cache-Control": "Always `no-store`; this service has no cacheable GET.",
    "X-Request-Id": (
        "Controlled echo of the caller's X-Request-Id, or a freshly minted "
        "`req-<uuid4hex>` when absent or malformed."
    ),
    "X-Trace-Id": "Correlation id, mirrored into the response body's `trace_id`.",
    "WWW-Authenticate": (
        f"`{CHALLENGE_S2S}` on service-to-service paths, `{CHALLENGE_APIKEY}` on the "
        "two inbound wedap callback paths."
    ),
    "Retry-After": "Seconds to wait before retrying the readiness probe.",
}


class OpenApiContractError(RuntimeError):
    """A route has no declaration, or a declaration has no route.

    Raised rather than skipped: an operation the generator quietly waves through is
    an operation whose contract nobody reviewed, which is the exact failure this
    module exists to prevent.
    """


def authentication_challenge(app: FastAPI, path: str) -> str | None:
    """The ``WWW-Authenticate`` value ``path`` answers 401 with, or ``None``.

    Derived from the **mounted** :class:`~app.core.s2s.S2SMiddleware` instance rather
    than from a copy of its configuration, so this cannot drift from what the running
    stack does. Path normalisation matches ``S2SMiddleware.dispatch`` exactly
    (``rstrip("/")`` with ``"/"`` preserved).

    Returns ``None`` for the exempt paths, which have no 401 at all.
    """
    from app.core.s2s import S2SMiddleware

    for middleware in app.user_middleware:
        # `Middleware.cls` is typed as a generic factory, so compare by identity
        # through `object` rather than fighting the annotation.
        if cast(object, middleware.cls) is not cast(object, S2SMiddleware):
            continue
        kwargs: dict[str, Any] = middleware.kwargs
        exempt: set[str] = kwargs["exempt_paths"]
        callbacks: set[str] = kwargs.get("callback_paths") or set()
        normalized = path.rstrip("/") or "/"
        if normalized in exempt:
            return None
        if normalized in callbacks:
            return CHALLENGE_APIKEY
        return CHALLENGE_S2S
    raise OpenApiContractError("S2SMiddleware is not mounted; cannot classify 401 surface")


def iter_operations(app: FastAPI) -> Iterator[tuple[str, str]]:
    """Every ``(METHOD, path)`` the app really serves as an OpenAPI operation.

    Delegates to :func:`app.core.openapi_status_scan.route_targets`, which walks the
    same route contexts ``get_openapi`` does, so this enumeration and the generated
    document cannot disagree about what exists.
    """
    from app.core.openapi_status_scan import route_targets

    for target in route_targets(app):
        yield target.method, target.path


def _response_object(
    contract: OperationContract,
    status: int,
    *,
    default_response: dict[str, Any] | None,
) -> dict[str, Any]:
    """One entry of an operation's ``responses`` map.

    Success reuses FastAPI's generated object (its ``content`` already carries the
    real ``response_model`` schema); failures get the error-envelope schema at the
    media type this service actually serves.
    """
    if status == contract.success_status and default_response is not None:
        response: dict[str, Any] = copy.deepcopy(default_response)
        response.setdefault("description", _STATUS_DESCRIPTIONS[status])
    else:
        response = {
            "description": _STATUS_DESCRIPTIONS[status],
            "content": {
                contract.error_media_type: {
                    "schema": {"$ref": f"#/components/schemas/{_ERROR_ENVELOPE_SCHEMA_NAME}"}
                }
            },
        }
    response["headers"] = {
        name: {
            "description": _HEADER_DESCRIPTIONS[name],
            "required": True,
            "schema": {"type": "string"},
        }
        for name in contract.headers_for(status)
    }
    return response


def apply_contracts(app: FastAPI, schema: dict[str, Any]) -> dict[str, Any]:
    """Rewrite every operation's ``responses`` from :data:`OPERATION_CONTRACTS`.

    Mutates and returns ``schema``. Fails closed on either kind of drift: a served
    operation with no declaration, or a declaration whose operation no longer exists.
    """
    paths: dict[str, dict[str, Any]] = schema.get("paths", {})
    seen: set[tuple[str, str]] = set()
    for path, path_item in paths.items():
        for method, operation in path_item.items():
            key = (method.upper(), path)
            if key not in OPERATION_CONTRACTS:
                raise OpenApiContractError(
                    f"{key[0]} {key[1]} is served but absent from OPERATION_CONTRACTS -- "
                    "declare its success status, reachable failure statuses and headers"
                )
            seen.add(key)
            contract = OPERATION_CONTRACTS[key]
            default_responses: dict[str, Any] = operation.get("responses", {})
            operation["responses"] = {
                str(status): _response_object(
                    contract,
                    status,
                    default_response=default_responses.get(str(status)),
                )
                for status in contract.declared_statuses()
            }
            operation["x-lending-unknown-query-parameters"] = contract.unknown_query
            operation["x-lending-problem-compliance"] = contract.problem_compliant
            operation["x-lending-exception-ref"] = PROBLEM_COMPLIANCE_EXCEPTION_REF
    stale = sorted(set(OPERATION_CONTRACTS) - seen)
    if stale:
        raise OpenApiContractError(
            f"OPERATION_CONTRACTS declares operations the app no longer serves: {stale}"
        )
    components: dict[str, Any] = schema.setdefault("components", {})
    schemas: dict[str, Any] = components.setdefault("schemas", {})
    for name, definition in _ERROR_SCHEMAS.items():
        schemas[name] = copy.deepcopy(definition)
    _drop_unreferenced(schema, schemas, _FASTAPI_VALIDATION_SCHEMAS)
    return schema


#: FastAPI injects these two whenever any operation declares a 422. Every 422 in this
#: build is served as the ``err()`` envelope by ``_validation_exception_handler``, so
#: once the responses are rewritten nothing points at them -- and leaving them behind
#: would tell a reader the 422 body is an ``HTTPValidationError``, which is false.
_FASTAPI_VALIDATION_SCHEMAS: tuple[str, ...] = ("HTTPValidationError", "ValidationError")


def _drop_unreferenced(
    schema: dict[str, Any], schemas: dict[str, Any], names: tuple[str, ...]
) -> None:
    """Remove each of ``names`` from ``schemas`` if the document no longer ``$ref``s it.

    Order matters: ``HTTPValidationError`` references ``ValidationError``, so the
    former must go first for the latter to become unreferenced. Anything still
    referenced is put straight back -- this prunes dead weight, it never breaks a link.
    """
    for name in names:
        definition = schemas.pop(name, None)
        if definition is None:
            continue
        if f'"#/components/schemas/{name}"' in json.dumps(schema):
            schemas[name] = definition


def build_openapi(app: FastAPI) -> dict[str, Any]:
    """The app's OpenAPI document with contract-accurate ``responses``.

    Installed as ``app.openapi`` in ``create_app``. The result is cached on the app
    exactly the way FastAPI caches its own, so ``/openapi.json`` stays cheap.
    """
    cached: dict[str, Any] | None = getattr(app, "_lending_openapi_schema", None)
    if cached is not None:
        return cached
    app.openapi_schema = None
    schema = apply_contracts(app, FastAPI.openapi(app))
    app.openapi_schema = schema
    app._lending_openapi_schema = schema  # type: ignore[attr-defined]
    return schema
