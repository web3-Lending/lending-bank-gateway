"""状态查询 API 测试：GET /status + GET /{biz_seq_no}/steps。"""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.clients.wedap import WedapError
from app.main import create_app
from app.models.base import Base

# ── 公共常量 ──────────────────────────────────────────────────────────────────

HEADERS = {
    "X-Caller-Service": "lifecycle",
    "X-Tenant-Id": "OCBC",
    "X-Request-Id": "req-status-1",
    "Idempotency-Key": "CLT-20260611-0001234567890",
}

COLLECT_BODY = {
    "bizSeqNo": "CLT-20260611-0001234567890",
    "totalAmount": "500.0000",
    "currencyCode": "USD",
    "userList": [{"userId": "U1", "amount": "500.0000"}],
}

STATUS_URL = "/api/v1/bank-funds/status"
STEPS_URL_TEMPLATE = "/api/v1/composite-transactions/{biz_seq_no}/steps"


# ── Fixtures ──────────────────────────────────────────────────────────────────


async def _create_tables(engine) -> None:  # type: ignore[no-untyped-def]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.fixture()
def client() -> TestClient:
    app = create_app()
    asyncio.run(_create_tables(app.state.engine))
    wedap = AsyncMock()
    wedap.collect_from_users.return_value = {"txnStatus": "PROCESSING"}
    wedap.distribute_to_users.return_value = {"txnStatus": "PROCESSING"}
    wedap.query_funds_status.return_value = {
        "txnStatus": "SUBMITTED",
        "bizSeqNo": COLLECT_BODY["bizSeqNo"],
    }
    wedap.get_composite_steps.return_value = [
        {"stepSeq": 1, "stepType": "DEBIT", "status": "SUCCESS"},
        {"stepSeq": 2, "stepType": "CREDIT", "status": "SUCCESS"},
    ]
    app.state.wedap = wedap
    return TestClient(app)


# ── Test 1：种单后 status 查询返回 orderStatus + wedap 合成视图 ─────────────────


def test_status_after_submit_returns_order_and_wedap(client: TestClient) -> None:
    """先 POST 种单，GET /status 返回 orderStatus==SUBMITTED + wedap.txnStatus 来自 mock。"""
    # 种单
    client.post("/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS)

    r = client.get(
        STATUS_URL,
        params={"bizSeqNo": COLLECT_BODY["bizSeqNo"]},
        headers=HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    data = body["data"]
    assert data["orderStatus"] == "SUBMITTED"
    assert data["bizSeqNo"] == COLLECT_BODY["bizSeqNo"]
    assert data["wedap"]["txnStatus"] == "SUBMITTED"


# ── Test 2：未知 bizSeqNo → 404 GW_404_ORDER ─────────────────────────────────


def test_status_unknown_biz_seq_no_404(client: TestClient) -> None:
    """未知 bizSeqNo → 404 GW_404_ORDER。"""
    r = client.get(
        STATUS_URL,
        params={"bizSeqNo": "CLT-20260611-NOTEXIST0001"},
        headers=HEADERS,
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "GW_404_ORDER"


# ── Test 3：跨 tenant 隔离 ─────────────────────────────────────────────────────


def test_status_cross_tenant_isolation_404(client: TestClient) -> None:
    """tenant A 种单，tenant B 查 → 404（跨租户不可见）。"""
    # tenant A 种单
    client.post("/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS)

    # tenant B 查询
    headers_b = {**HEADERS, "X-Tenant-Id": "DBS", "X-Request-Id": "req-status-b"}
    r = client.get(
        STATUS_URL,
        params={"bizSeqNo": COLLECT_BODY["bizSeqNo"]},
        headers=headers_b,
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "GW_404_ORDER"


# ── Test 4：wedap 查询超时 → 200 + unavailable=True ──────────────────────────


def test_status_wedap_timeout_degrades_gracefully(client: TestClient) -> None:
    """wedap query_funds_status 超时 → HTTP 200 且 data.wedap.unavailable is True。"""
    # 种单
    client.post("/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS)

    # 让 wedap 超时
    client.app.state.wedap.query_funds_status.side_effect = httpx.TimeoutException("timeout")  # type: ignore[union-attr]

    r = client.get(
        STATUS_URL,
        params={"bizSeqNo": COLLECT_BODY["bizSeqNo"]},
        headers=HEADERS,
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["wedap"]["unavailable"] is True
    assert "reason" in data["wedap"]


# ── Test 4b：wedap TransportError 也降级 ─────────────────────────────────────


def test_status_wedap_transport_error_degrades_gracefully(client: TestClient) -> None:
    """wedap query_funds_status TransportError → HTTP 200 + unavailable=True。"""
    client.post("/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS)
    client.app.state.wedap.query_funds_status.side_effect = httpx.TransportError("conn reset")  # type: ignore[union-attr]

    r = client.get(
        STATUS_URL,
        params={"bizSeqNo": COLLECT_BODY["bizSeqNo"]},
        headers=HEADERS,
    )
    assert r.status_code == 200
    assert r.json()["data"]["wedap"]["unavailable"] is True


# ── Test 4c：wedap WedapError 也降级 ─────────────────────────────────────────


def test_status_wedap_error_degrades_gracefully(client: TestClient) -> None:
    """wedap query_funds_status WedapError → HTTP 200 + unavailable=True。"""
    client.post("/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS)
    client.app.state.wedap.query_funds_status.side_effect = WedapError("500", "internal")  # type: ignore[union-attr]

    r = client.get(
        STATUS_URL,
        params={"bizSeqNo": COLLECT_BODY["bizSeqNo"]},
        headers=HEADERS,
    )
    assert r.status_code == 200
    assert r.json()["data"]["wedap"]["unavailable"] is True


# ── Test 5：composite steps 透传 ─────────────────────────────────────────────


def test_composite_steps_passthrough(client: TestClient) -> None:
    """先种单，mock get_composite_steps 返回两 leg，断言 data.steps 原样透传。"""
    # 先种单（本地守卫需要 order 存在）
    client.post("/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS)

    biz_seq_no = COLLECT_BODY["bizSeqNo"]
    r = client.get(
        STEPS_URL_TEMPLATE.format(biz_seq_no=biz_seq_no),
        headers=HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    steps = body["data"]["steps"]
    assert len(steps) == 2
    assert steps[0]["stepSeq"] == 1
    assert steps[1]["stepType"] == "CREDIT"


# ── Test 5b：steps 未知单 → 404 GW_404_ORDER ─────────────────────────────────


def test_steps_unknown_order_404(client: TestClient) -> None:
    """steps 端点：未知 bizSeqNo（本地无记录） → 404 GW_404_ORDER。"""
    r = client.get(
        STEPS_URL_TEMPLATE.format(biz_seq_no="CLT-20260611-NOTEXIST0001"),
        headers=HEADERS,
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "GW_404_ORDER"


# ── Test 5c：steps 跨 tenant 隔离 → 404 ──────────────────────────────────────


def test_steps_cross_tenant_isolation_404(client: TestClient) -> None:
    """tenant A 种单，tenant B 查 steps → 404（跨租户不可见）。"""
    # tenant A 种单
    client.post("/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS)

    # tenant B 查询 steps
    headers_b = {**HEADERS, "X-Tenant-Id": "DBS", "X-Request-Id": "req-steps-b"}
    r = client.get(
        STEPS_URL_TEMPLATE.format(biz_seq_no=COLLECT_BODY["bizSeqNo"]),
        headers=headers_b,
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "GW_404_ORDER"


# ── Test 5d：steps wedap 超时 → 502 GW_502_UPSTREAM ──────────────────────────


def test_steps_wedap_timeout_502(client: TestClient) -> None:
    """steps 端点：wedap get_composite_steps 超时 → 502 GW_502_UPSTREAM。"""
    # 先种单
    client.post("/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS)

    # 让 wedap 超时
    client.app.state.wedap.get_composite_steps.side_effect = httpx.TimeoutException("timeout")  # type: ignore[union-attr]

    r = client.get(
        STEPS_URL_TEMPLATE.format(biz_seq_no=COLLECT_BODY["bizSeqNo"]),
        headers=HEADERS,
    )
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "GW_502_UPSTREAM"


# ── Test 5e：steps wedap TransportError → 502 ─────────────────────────────────


def test_steps_wedap_transport_error_502(client: TestClient) -> None:
    """steps 端点：wedap get_composite_steps TransportError → 502 GW_502_UPSTREAM。"""
    # 先种单
    client.post("/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS)

    # 让 wedap 网络错误
    client.app.state.wedap.get_composite_steps.side_effect = httpx.TransportError("conn reset")  # type: ignore[union-attr]

    r = client.get(
        STEPS_URL_TEMPLATE.format(biz_seq_no=COLLECT_BODY["bizSeqNo"]),
        headers=HEADERS,
    )
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "GW_502_UPSTREAM"


# ── Test 6：缺 X-Tenant-Id → 400 ─────────────────────────────────────────────


def test_status_missing_tenant_id_400(client: TestClient) -> None:
    """缺 X-Tenant-Id header → 400 GW_400_HEADER。"""
    h = {k: v for k, v in HEADERS.items() if k != "X-Tenant-Id"}
    r = client.get(
        STATUS_URL,
        params={"bizSeqNo": COLLECT_BODY["bizSeqNo"]},
        headers=h,
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "GW_400_HEADER"


def test_steps_missing_tenant_id_400(client: TestClient) -> None:
    """composite steps 缺 X-Tenant-Id → 400 GW_400_HEADER。"""
    h = {k: v for k, v in HEADERS.items() if k != "X-Tenant-Id"}
    r = client.get(
        STEPS_URL_TEMPLATE.format(biz_seq_no=COLLECT_BODY["bizSeqNo"]),
        headers=h,
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "GW_400_HEADER"
