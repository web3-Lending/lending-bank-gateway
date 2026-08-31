"""Unit coverage for the corners of the contract generator the gates cannot reach.

Three of them matter beyond the coverage number:

* ``authentication_challenge`` must refuse to guess when ``S2SMiddleware`` is not
  mounted. Returning "no challenge" there would silently turn the 401 surface of a
  misconfigured app into "this app has no authentication", and the snapshot would
  faithfully record that lie.
* ``_drop_unreferenced`` must only prune what nothing points at. Pruning a schema
  that is still referenced would produce a document with a dangling ``$ref``.
* ``build_openapi`` caches; the cached document must be the same object the first
  call produced, or ``/openapi.json`` could drift from what the gates checked.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, HTTPException

from app.core.config import Settings
from app.core.openapi_contract import (
    CHALLENGE_APIKEY,
    CHALLENGE_S2S,
    OPERATION_CONTRACTS,
    UNIVERSAL_RESPONSE_HEADERS,
    OpenApiContractError,
    _drop_unreferenced,
    authentication_challenge,
    build_openapi,
)
from app.main import create_app


def test_authentication_challenge_classifies_the_three_kinds_of_path() -> None:
    app = create_app()
    assert authentication_challenge(app, "/healthz") is None
    assert authentication_challenge(app, "/api/v1/recon/notify") == CHALLENGE_APIKEY
    assert authentication_challenge(app, "/api/v1/bank-funds/status") == CHALLENGE_S2S


def test_authentication_challenge_normalises_a_trailing_slash() -> None:
    """Matches ``S2SMiddleware.dispatch`` exactly, or exempt paths would drift."""
    app = create_app()
    assert authentication_challenge(app, "/healthz/") is None


def test_authentication_challenge_refuses_to_guess_without_the_middleware() -> None:
    with pytest.raises(OpenApiContractError, match="S2SMiddleware is not mounted"):
        authentication_challenge(FastAPI(), "/healthz")


def test_drop_unreferenced_keeps_a_schema_that_is_still_pointed_at() -> None:
    schemas: dict[str, Any] = {"Kept": {"type": "object"}}
    document: dict[str, Any] = {
        "components": {"schemas": schemas},
        "paths": {"/x": {"get": {"$ref": "#/components/schemas/Kept"}}},
    }
    _drop_unreferenced(document, schemas, ("Kept",))
    assert schemas == {"Kept": {"type": "object"}}


def test_drop_unreferenced_removes_an_orphan_and_ignores_absent_names() -> None:
    schemas: dict[str, Any] = {"Orphan": {"type": "object"}}
    document: dict[str, Any] = {"components": {"schemas": schemas}, "paths": {}}
    _drop_unreferenced(document, schemas, ("Orphan", "NeverExisted"))
    assert schemas == {}


def test_build_openapi_serves_the_same_cached_document() -> None:
    app = create_app()
    first = build_openapi(app)
    assert build_openapi(app) is first
    assert app.openapi() is first


def test_headers_for_merges_the_universal_and_status_specific_sets() -> None:
    readyz = OPERATION_CONTRACTS[("GET", "/readyz")]
    assert readyz.headers_for(503) == tuple(sorted({*UNIVERSAL_RESPONSE_HEADERS, "Retry-After"}))
    assert readyz.headers_for(200) == tuple(sorted(UNIVERSAL_RESPONSE_HEADERS))


def test_declared_statuses_are_sorted_and_include_the_success_status() -> None:
    contract = OPERATION_CONTRACTS[("POST", "/api/v1/bank-funds/refunds")]
    statuses = contract.declared_statuses()
    assert statuses == tuple(sorted(statuses))
    assert contract.success_status in statuses


def test_generator_runs_against_an_app_built_from_explicit_settings() -> None:
    """A configured app serves the same 28 operations as the default one."""
    app = create_app(Settings(wedap_callback_api_key="unit-key"))
    assert len(build_openapi(app)["paths"]) == len({path for _method, path in OPERATION_CONTRACTS})


# ── query-location guard, unit level ─────────────────────────────────────────


def test_query_location_params_validates_when_state_is_unset() -> None:
    """A handler exercised outside the application still gets a validated mapping.

    The application-level dependency normally fills ``request.state.query_location``
    before any handler runs. A direct unit call has no such dependency, and the
    fallback must validate rather than hand back raw, unchecked query parameters --
    otherwise the one path that skips the guard is the one a unit test exercises.
    """
    from starlette.requests import Request

    from app.core.query_location import query_location_params

    def _request(query: str) -> Request:
        return Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/probe",
                "headers": [],
                "query_string": query.encode(),
            }
        )

    assert query_location_params(_request("a=1&b=2")) == {"a": "1", "b": "2"}

    with pytest.raises(HTTPException) as duplicated:
        query_location_params(_request("a=1&a=2"))
    assert duplicated.value.detail["code"] == "GW_422_DUPLICATE_QUERY_PARAMETER"

    # And once the state slot is filled, the stored mapping is returned as-is.
    request = _request("a=1")
    assert query_location_params(request) == {"a": "1"}
    assert query_location_params(request) == {"a": "1"}
