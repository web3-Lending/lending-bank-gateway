"""北向银行资金 API 测试：collect-from-users + distribute-to-users。"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models.base import Base

HEADERS = {
    "X-Caller-Service": "lifecycle",
    "X-Tenant-Id": "OCBC",
    "X-Request-Id": "req-1",
    "Idempotency-Key": "CLT-20260611-0001234567890",
}
COLLECT_BODY = {
    "bizSeqNo": "CLT-20260611-0001234567890",
    "totalAmount": "500.0000",
    "currencyCode": "USD",
    "userList": [{"userId": "U1", "amount": "500.0000"}],
}
DISTRIBUTE_BODY = {
    "bizSeqNo": "DST-20260611-0001234567890",
    "totalAmount": "200.0000",
    "currencyCode": "USD",
    "userList": [{"userId": "U2", "amount": "200.0000"}],
}


async def _create_tables(engine) -> None:  # type: ignore[no-untyped-def]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.fixture()
def client() -> TestClient:
    app = create_app()
    asyncio.get_event_loop().run_until_complete(_create_tables(app.state.engine))
    wedap = AsyncMock()
    wedap.collect_from_users.return_value = {"txnStatus": "PROCESSING"}
    wedap.distribute_to_users.return_value = {"txnStatus": "PROCESSING"}
    app.state.wedap = wedap
    return TestClient(app)


# ── collect-from-users ────────────────────────────────────────────────────────


def test_collect_accepted_envelope(client: TestClient) -> None:
    """受理 → 200 + success=True + txnStatus=PROCESSING + bizSeqNo 回显。"""
    r = client.post("/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["data"]["txnStatus"] == "PROCESSING"
    assert body["data"]["bizSeqNo"] == COLLECT_BODY["bizSeqNo"]
    assert body["trace_id"]


def test_collect_idempotent_replay_no_extra_call(client: TestClient) -> None:
    """同 key 同 payload 二次提交：外呼只发生 1 次。"""
    client.post("/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS)
    client.post("/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS)
    assert client.app.state.wedap.collect_from_users.await_count == 1  # type: ignore[union-attr]


def test_collect_missing_total_amount_422(client: TestClient) -> None:
    """totalAmount 字段缺失 → Pydantic 必填校验 → 422。"""
    body_no_amount = {k: v for k, v in COLLECT_BODY.items() if k != "totalAmount"}
    r = client.post("/api/v1/bank-funds/collect-from-users", json=body_no_amount, headers=HEADERS)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "GW_422_VALIDATION"


def test_collect_invalid_amount_str_400(client: TestClient) -> None:
    """totalAmount 无法解析为 Decimal → 400 GW_400_VALIDATION。"""
    body_bad = {**COLLECT_BODY, "totalAmount": "not-a-number"}
    r = client.post("/api/v1/bank-funds/collect-from-users", json=body_bad, headers=HEADERS)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "GW_400_VALIDATION"


def test_collect_bad_biz_seq_no_400(client: TestClient) -> None:
    """bizSeqNo 格式不合规 → submit_order 内 ValueError → 400 GW_400_VALIDATION。"""
    bad_key = "WB-1704067200000-COLLECT-10-0001-123456"
    body_bad = {**COLLECT_BODY, "bizSeqNo": bad_key}
    h = {**HEADERS, "Idempotency-Key": bad_key}
    r = client.post("/api/v1/bank-funds/collect-from-users", json=body_bad, headers=h)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "GW_400_VALIDATION"


# ── distribute-to-users ───────────────────────────────────────────────────────


def test_distribute_accepted_envelope(client: TestClient) -> None:
    """受理 → 200 + success=True + txnStatus=PROCESSING + bizSeqNo 回显。"""
    h = {**HEADERS, "Idempotency-Key": DISTRIBUTE_BODY["bizSeqNo"], "X-Request-Id": "req-2"}
    r = client.post("/api/v1/bank-funds/distribute-to-users", json=DISTRIBUTE_BODY, headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["data"]["txnStatus"] == "PROCESSING"
    assert body["data"]["bizSeqNo"] == DISTRIBUTE_BODY["bizSeqNo"]
    assert body["trace_id"]


def test_distribute_idempotent_replay_no_extra_call(client: TestClient) -> None:
    """同 key 同 payload 二次提交：外呼只发生 1 次。"""
    h = {**HEADERS, "Idempotency-Key": DISTRIBUTE_BODY["bizSeqNo"], "X-Request-Id": "req-dst"}
    client.post("/api/v1/bank-funds/distribute-to-users", json=DISTRIBUTE_BODY, headers=h)
    client.post("/api/v1/bank-funds/distribute-to-users", json=DISTRIBUTE_BODY, headers=h)
    assert (
        client.app.state.wedap.distribute_to_users.await_count == 1  # type: ignore[union-attr]
    )


def test_collect_same_key_different_payload_409(client: TestClient) -> None:
    """同 Idempotency-Key 不同 payload → 409 GW_409_IDEMPOTENCY。"""
    client.post("/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS)
    mutated = {**COLLECT_BODY, "totalAmount": "999.0000"}
    r = client.post("/api/v1/bank-funds/collect-from-users", json=mutated, headers=HEADERS)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "GW_409_IDEMPOTENCY"
