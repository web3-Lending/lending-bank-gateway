"""部署验收发现的缺陷回归测试。

缺陷 1：读路径未捕获 httpx.HTTPStatusError → 上游 4xx/5xx 应返回 502 GW_502_UPSTREAM
  - deposit 端点（_audited_passthrough）
  - /status 端点（降级路径，HTTP 200 + wedap unavailable）

缺陷 2：500 响应 trace_id="trc-none"
  - ServerErrorMiddleware 在 IdentifierMiddleware 外层，contextvar 已 reset
  - 应从请求头 X-Trace-Id 回退
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models.base import Base

# ── 公共常量 ──────────────────────────────────────────────────────────────────

HEADERS = {
    "X-Caller-Service": "lifecycle",
    "X-Tenant-Id": "OCBC",
    "X-Request-Id": "req-finding-1",
    "Idempotency-Key": "CLT-20260611-0001234567890",
}

COLLECT_BODY = {
    "bizSeqNo": "CLT-20260611-0001234567890",
    "transType": "LOAN_COLLECT",
    "totalAmount": "500.0000",
    "currencyCode": "USD",
    "userList": [{"userId": "U1", "amount": "500.0000"}],
}


async def _create_tables(engine) -> None:  # type: ignore[no-untyped-def]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _make_http_status_error(status_code: int) -> httpx.HTTPStatusError:
    """构造 httpx.HTTPStatusError，模拟上游返回 HTTP 错误状态码。"""
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    request = MagicMock(spec=httpx.Request)
    return httpx.HTTPStatusError(
        f"HTTP {status_code}",
        request=request,
        response=response,
    )


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def deposit_client() -> TestClient:
    app = create_app()
    asyncio.run(_create_tables(app.state.engine))
    wedap = AsyncMock()
    wedap.get_deposit_balance_total.return_value = {"accounts": []}
    app.state.wedap = wedap
    return TestClient(app)


@pytest.fixture()
def status_client() -> TestClient:
    app = create_app()
    asyncio.run(_create_tables(app.state.engine))
    wedap = AsyncMock()
    wedap.collect_from_users.return_value = {"txnStatus": "PROCESSING"}
    wedap.query_transaction_status.return_value = {"txnStatus": "SUBMITTED"}
    app.state.wedap = wedap
    return TestClient(app)


# ── 缺陷 1：deposit 端点 HTTPStatusError → 502 ────────────────────────────────


def test_deposit_upstream_401_returns_502(deposit_client: TestClient) -> None:
    """上游 wedap 返回 401 → deposit 端点应返回 502 GW_502_UPSTREAM（不是 500）。"""
    deposit_client.app.state.wedap.get_deposit_balance_total.side_effect = (  # type: ignore[union-attr]
        _make_http_status_error(401)
    )
    r = deposit_client.get(
        "/api/v1/deposit/balances/total",
        params={"userId": "U1"},
        headers=HEADERS,
    )
    assert r.status_code == 502
    body = r.json()
    assert body["error"]["code"] == "GW_502_UPSTREAM"
    assert "401" in body["error"]["message"]


def test_deposit_upstream_503_returns_502(deposit_client: TestClient) -> None:
    """上游 wedap 返回 503 → deposit 端点应返回 502 GW_502_UPSTREAM。"""
    deposit_client.app.state.wedap.get_deposit_balance_total.side_effect = (  # type: ignore[union-attr]
        _make_http_status_error(503)
    )
    r = deposit_client.get(
        "/api/v1/deposit/balances/total",
        params={"userId": "U1"},
        headers=HEADERS,
    )
    assert r.status_code == 502
    body = r.json()
    assert body["error"]["code"] == "GW_502_UPSTREAM"
    # 消息中含 HTTP 状态码，不泄露 wedap 响应体
    assert "503" in body["error"]["message"]


# ── 缺陷 1：/status 端点 HTTPStatusError → 200 降级 ──────────────────────────


def test_status_upstream_http_error_degrades_gracefully(status_client: TestClient) -> None:
    """bank-funds/status：wedap 返回 HTTP 错误 → HTTP 200 + wedap.unavailable=True（降级）。"""
    # 先种单
    status_client.post("/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS)

    status_client.app.state.wedap.query_transaction_status.side_effect = (  # type: ignore[union-attr]
        _make_http_status_error(503)
    )

    r = status_client.get(
        "/api/v1/bank-funds/status",
        params={"bizSeqNo": COLLECT_BODY["bizSeqNo"]},
        headers=HEADERS,
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["wedap"]["unavailable"] is True


# ── 缺陷 2：500 响应 trace_id 从请求头回退 ────────────────────────────────────


def test_500_trace_id_falls_back_to_header(app) -> None:  # type: ignore[no-untyped-def]
    """未捕获异常触发 500 时，若 contextvar 已 reset，应从 X-Trace-Id 头回退。"""

    @app.get("/test-500-trace-id")
    async def _boom() -> dict:
        raise ValueError("trigger 500")

    client = TestClient(app, raise_server_exceptions=False)
    r = client.get(
        "/test-500-trace-id",
        headers={"X-Caller-Service": "test", "X-Trace-Id": "test-trace-123"},
    )
    assert r.status_code == 500
    body = r.json()
    assert body["error"]["code"] == "GW_500_INTERNAL"
    assert body["trace_id"] == "test-trace-123"
