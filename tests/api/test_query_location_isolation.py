"""API-HTTP-022 · query / body / header are separate OpenAPI locations.

What "location isolation" means here
------------------------------------
v2.2 API-HTTP-021 clause 3 and API-HTTP-022 say query, form body, header and cookie
are validated **per location**, and §7.2.1 clause 7 adds that this service, as the
first controlled boundary, does the validating rather than deferring it to the
origin. Two properties follow, and this module holds both to their behaviour:

1. **Inside one location, nothing collapses silently.** ``?a=1&a=2`` on a scalar is a
   duplicate the caller must be told about (422), not a value the framework quietly
   picks the last of. The 2026-08-27 inventory measured last-wins on two operations
   (report §241) and, worse, measured the collapsed value reaching the database.
2. **Across locations, nothing substitutes or leaks.** A value in the query location
   may not fill a header-located input, a header may not fill a query-located
   parameter, and the seven wedap passthroughs may not re-emit caller-controlled
   query names that this gateway itself owns in the *header* location of the very
   same upstream call (report §300: ``dict(request.query_params)`` was forwarded to
   the bank verbatim, so ``?X-Tenant-Id=OTHER`` arrived alongside the gateway's own
   ``X-Tenant-Id`` header and the bank got to pick).

The control cases at the bottom are deliberate: without them "no cross-location
merge" would also hold on a service that rejects everything, and the isolation
assertions would be incapable of failing.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models.base import Base

HEADERS = {
    "X-Caller-Service": "lifecycle",
    "X-Tenant-Id": "OCBC",
    "X-Request-Id": "req-loc-1",
}


async def _create_tables(engine: Any) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.fixture()
def client_and_wedap() -> tuple[TestClient, AsyncMock]:
    """A wired app whose upstream is a mock, so forwarded params can be read back."""
    app = create_app()
    asyncio.run(_create_tables(app.state.engine))
    wedap = AsyncMock()
    wedap.get_deposit_accounts.return_value = {"accounts": []}
    app.state.wedap = wedap
    return TestClient(app, raise_server_exceptions=False), wedap


# ── property 1 · no silent collapse inside the query location ────────────────


def test_duplicate_query_parameter_is_rejected_on_an_undeclared_name(
    client_and_wedap: tuple[TestClient, AsyncMock],
) -> None:
    """`?import_date=A&import_date=B` used to answer 200 carrying B (report §241)."""
    client, _ = client_and_wedap
    response = client.get(
        "/api/v1/admin/wedap-import/delivery-report?import_date=20260827&import_date=19990101",
        headers=HEADERS,
    )
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error"]["code"] == "GW_422_DUPLICATE_QUERY_PARAMETER"
    assert body["error"]["details"]["parameters"] == ["import_date"]


def test_duplicate_query_parameter_is_rejected_before_the_database_is_touched(
    client_and_wedap: tuple[TestClient, AsyncMock],
) -> None:
    """`?bizSeqNo=AAA&bizSeqNo=BBB` used to answer `404 order not found: BBB`.

    The 404 is the tell: producing it meant the collapsed second value had already
    been used for a ledger lookup, which §7.2.1 clause 6 forbids for a request that
    is not well-formed at the boundary.
    """
    client, _ = client_and_wedap
    response = client.get(
        "/api/v1/bank-funds/status?bizSeqNo=AAA-FIRST&bizSeqNo=BBB-SECOND",
        headers=HEADERS,
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "GW_422_DUPLICATE_QUERY_PARAMETER"
    # The rejected value must not be echoed back -- the inventory found `%C0%AF`
    # reflected into an error message on this same endpoint.
    assert "BBB-SECOND" not in response.text


def test_duplicate_query_parameter_lists_every_offending_name_once() -> None:
    """Two duplicated names are both reported, and neither is reported twice."""
    client = TestClient(create_app(), raise_server_exceptions=False)
    response = client.get(
        "/healthz?alpha=1&alpha=2&beta=1&beta=2&beta=3&gamma=1",
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["details"]["parameters"] == ["alpha", "beta"]


# ── property 2 · no substitution or leakage across locations ─────────────────


def test_a_header_named_query_parameter_is_rejected(
    client_and_wedap: tuple[TestClient, AsyncMock],
) -> None:
    """`?X-Tenant-Id=OTHER` used to be forwarded to the bank next to the real header."""
    client, wedap = client_and_wedap
    response = client.get(
        "/api/v1/deposit/accounts?X-Tenant-Id=EVIL-TENANT",
        headers=HEADERS,
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "GW_422_QUERY_LOCATION_CONFLICT"
    assert wedap.get_deposit_accounts.await_count == 0, (
        "a location-confused request must be rejected before the upstream call"
    )


def test_header_named_query_parameter_is_rejected_case_insensitively(
    client_and_wedap: tuple[TestClient, AsyncMock],
) -> None:
    """HTTP field names are case-insensitive, so the guard must be too."""
    client, wedap = client_and_wedap
    response = client.get(
        "/api/v1/deposit/accounts?x-request-id=forged",
        headers=HEADERS,
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["details"]["parameters"] == ["x-request-id"]
    assert wedap.get_deposit_accounts.await_count == 0


def test_passthrough_forwards_only_query_located_values(
    client_and_wedap: tuple[TestClient, AsyncMock],
) -> None:
    """The upstream query location carries the caller's query and nothing else.

    Header-located identity (`X-Tenant-Id` / `X-Request-Id`) travels upstream as
    headers, set by `WedapClient._headers`; it must never be duplicated into the
    upstream *query* by way of the passthrough dict.
    """
    client, wedap = client_and_wedap
    response = client.get(
        "/api/v1/deposit/accounts?userId=U1&channelId=C1",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    forwarded = wedap.get_deposit_accounts.await_args.kwargs["params"]
    assert forwarded == {"userId": "U1", "channelId": "C1"}


# ── controls · the isolation assertions above must be able to fail ───────────


def test_control_query_cannot_satisfy_a_header_located_input(
    client_and_wedap: tuple[TestClient, AsyncMock],
) -> None:
    """`?tenantId=...` (a name this service does not own in any location) is still
    not allowed to stand in for the `X-Tenant-Id` header.

    The rejection code changed on 2026-09-01 and the subject did not. Before, the
    query name was accepted and discarded, so the request died later at
    `require_headers` with `400 missing X-Tenant-Id`. Now the closed-world query
    contract refuses the undeclared name first, with `422`. Either way the query
    location did not satisfy a header-located input — which is the whole point of
    this control. Asserting on the earlier code would pin the ordering rather than
    the property.
    """
    client, _ = client_and_wedap
    response = client.get(
        "/api/v1/deposit/accounts?tenantId=OCBC",
        headers={"X-Caller-Service": "lifecycle", "X-Request-Id": "req-loc-2"},
    )
    assert response.status_code == 422, response.text
    body = response.json()["error"]
    assert body["code"] == "GW_422_UNKNOWN_QUERY_PARAMETER"
    assert body["details"]["parameters"] == ["tenantId"]
    # 反面：请求确实没有被当成「租户已给出」而放行 —— 上游一次都没被调到。
    assert response.status_code != 200


def test_control_header_cannot_satisfy_a_query_located_parameter(
    client_and_wedap: tuple[TestClient, AsyncMock],
) -> None:
    """`bizSeqNo` is declared `in: query`; sending it as a header must not bind it."""
    client, _ = client_and_wedap
    response = client.get(
        "/api/v1/bank-funds/status",
        headers={**HEADERS, "bizSeqNo": "SENT-AS-A-HEADER"},
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "GW_422_VALIDATION"


def test_control_a_single_valued_query_still_reaches_the_handler(
    client_and_wedap: tuple[TestClient, AsyncMock],
) -> None:
    """Without this, "duplicates are rejected" would also hold if every query were."""
    client, _ = client_and_wedap
    response = client.get(
        "/api/v1/admin/wedap-import/delivery-report?import_date=20260827",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["import_date"] == "20260827"


def test_control_repeated_value_of_the_same_name_is_still_a_duplicate() -> None:
    """`?a=1&a=1` is two fields, not one -- collapsing equal values would re-introduce
    exactly the silent merge this rule forbids."""
    response = TestClient(create_app(), raise_server_exceptions=False).get("/healthz?a=1&a=1")
    assert response.status_code == 422, response.text
    assert response.json()["error"]["details"]["parameters"] == ["a"]


def test_param_names_expands_a_pydantic_model_into_its_field_aliases() -> None:
    """`Annotated[SomeModel, Query()]` 展开成各字段名，不是外层那一个形参名。

    FastAPI 对「单个模型字段」形态会把模型展开成各字段来解析，所以 URL 上出现的是
    字段名（或其 alias）。守门若只看外层形参名，`?page=2` 这种明明写在签名里的字段
    会被自己的守门判成未声明参数而 422 —— 把合法请求拒掉。

    2026-09-01 补：这一支此前零覆盖（`app/core/query_location.py:90`），origin/main
    的覆盖率闸因此停在 99.95%。它是「只认外层名」和「按字段名认」的分水岭，
    改回只认外层名的话，除这条外没有任何用例会红。
    """
    from pydantic import BaseModel, Field

    from app.core.query_location import _param_names

    class _Page(BaseModel):
        page: int = 1
        size_alias: int = Field(20, alias="size")

    class _FieldInfo:
        annotation = _Page
        alias = "outer"

    class _Param:
        name = "outer"
        field_info = _FieldInfo()

    # 展开成字段面：alias 优先，没有 alias 的用字段名；外层 "outer" 不出现
    assert _param_names(_Param()) == {"page", "size"}


def test_param_names_falls_back_to_alias_for_a_plain_scalar_param() -> None:
    """反向鉴别力：非模型参数仍按 alias（无 alias 则形参名）认，不误走展开分支。"""
    from app.core.query_location import _param_names

    class _FieldInfo:
        annotation = int
        alias = "importDate"

    class _Param:
        name = "import_date"
        field_info = _FieldInfo()

    assert _param_names(_Param()) == {"importDate"}
