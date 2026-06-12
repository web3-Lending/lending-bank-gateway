"""北向贷款 API 测试：p2p-disbursements + p2p-repayments。"""

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
    "Idempotency-Key": "DSB-20260611-0001234567890",
}
BODY = {
    "bizSeqNo": "DSB-20260611-0001234567890",
    "channelId": "LEN",
    "transType": "DISBURSEMENT",
    "disbursementInfo": {
        "txnAmount": "100.0000",
        "currencyCode": "USD",
        "userId": "U1",
        "userName": "u",
    },
    "lenders": [{"userId": "L1"}],
}


@pytest.fixture()
def client() -> TestClient:
    app = create_app()
    # 建表（sqlite :memory: engine 已在 create_app 内创建，需在使用前 create_all）
    asyncio.run(_create_tables(app.state.engine))
    wedap = AsyncMock()
    wedap.submit_disbursement.return_value = {"txnStatus": "PROCESSING"}
    wedap.submit_repayment.return_value = {"txnStatus": "PROCESSING"}
    app.state.wedap = wedap
    return TestClient(app)


async def _create_tables(engine) -> None:  # type: ignore[no-untyped-def]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def test_disbursement_accepted_envelope(client: TestClient) -> None:
    r = client.post("/api/v1/loans/p2p-disbursements", json=BODY, headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True and body["data"]["txnStatus"] == "PROCESSING"
    assert body["data"]["bizSeqNo"] == BODY["bizSeqNo"] and body["trace_id"]


def test_missing_tenant_header_400(client: TestClient) -> None:
    h = {k: v for k, v in HEADERS.items() if k != "X-Tenant-Id"}
    r = client.post("/api/v1/loans/p2p-disbursements", json=BODY, headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == "GW_400_HEADER"


def test_missing_request_id_header_400(client: TestClient) -> None:
    h = {k: v for k, v in HEADERS.items() if k != "X-Request-Id"}
    r = client.post("/api/v1/loans/p2p-disbursements", json=BODY, headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == "GW_400_HEADER"


def test_bad_biz_seq_no_400(client: TestClient) -> None:
    bad = {**BODY, "bizSeqNo": "WB-1704067200000-DISB-10-0001-123456"}
    r = client.post(
        "/api/v1/loans/p2p-disbursements",
        json=bad,
        headers={**HEADERS, "Idempotency-Key": bad["bizSeqNo"]},
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "GW_400_VALIDATION"


def test_missing_amount_400(client: TestClient) -> None:
    """disbursementInfo 缺 txnAmount → 422 validation（Pydantic 必填）。"""
    body_no_amount = {
        **BODY,
        "disbursementInfo": {"currencyCode": "USD", "userId": "U1", "userName": "u"},
    }
    r = client.post("/api/v1/loans/p2p-disbursements", json=body_no_amount, headers=HEADERS)
    assert r.status_code == 422
    assert "VALIDATION" in r.json()["error"]["code"]


def test_invalid_amount_zero_400(client: TestClient) -> None:
    """txnAmount=0 → 400 GW_400_VALIDATION（非正数）。"""
    body_zero = {
        **BODY,
        "disbursementInfo": {**BODY["disbursementInfo"], "txnAmount": "0"},
    }
    r = client.post("/api/v1/loans/p2p-disbursements", json=body_zero, headers=HEADERS)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "GW_400_VALIDATION"


def test_same_key_different_payload_409(client: TestClient) -> None:
    client.post("/api/v1/loans/p2p-disbursements", json=BODY, headers=HEADERS)
    mutated = {
        **BODY,
        "disbursementInfo": {**BODY["disbursementInfo"], "txnAmount": "999.0000"},
    }
    r = client.post("/api/v1/loans/p2p-disbursements", json=mutated, headers=HEADERS)
    assert r.status_code == 409 and r.json()["error"]["code"] == "GW_409_IDEMPOTENCY"


def test_idempotent_replay_no_extra_call(client: TestClient) -> None:
    """同 key 同 payload 二次调用：wedap 外呼只发生 1 次（InFlight → PROCESSING）。"""
    client.post("/api/v1/loans/p2p-disbursements", json=BODY, headers=HEADERS)
    client.post("/api/v1/loans/p2p-disbursements", json=BODY, headers=HEADERS)
    # submit_disbursement 实际只在 submit_order 内通过 wedap_call 调用；
    # 第一次：InFlight → PROCESSING（mock 返回值），第二次 InFlight 直接走 PROCESSING 路径
    # 因为 session_factory 是 in-memory SQLite，第一次注册但 record_response 未调用 → InFlight
    assert client.app.state.wedap.submit_disbursement.await_count == 1  # type: ignore[union-attr]


def test_repayment_endpoint_works(client: TestClient) -> None:
    body = {
        "bizSeqNo": "RPY-20260611-0001234567890",
        "repaymentInfo": {"txnAmount": "50.0000", "currencyCode": "USD"},
    }
    r = client.post(
        "/api/v1/loans/p2p-repayments",
        json=body,
        headers={**HEADERS, "Idempotency-Key": body["bizSeqNo"]},
    )
    assert r.json()["data"]["bizSeqNo"] == body["bizSeqNo"]


def test_disbursement_idempotency_key_mismatch_400(client: TestClient) -> None:
    """Idempotency-Key header 存在且与 bizSeqNo 不一致 → 400 GW_400_IDEMPOTENCY_KEY。"""
    h = {**HEADERS, "Idempotency-Key": "WRONG-KEY-9999"}
    r = client.post("/api/v1/loans/p2p-disbursements", json=BODY, headers=h)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "GW_400_IDEMPOTENCY_KEY"


def test_disbursement_no_idempotency_key_header_passes(client: TestClient) -> None:
    """无 Idempotency-Key header → 放行，以 bizSeqNo 为准。"""
    h = {k: v for k, v in HEADERS.items() if k != "Idempotency-Key"}
    r = client.post("/api/v1/loans/p2p-disbursements", json=BODY, headers=h)
    assert r.status_code == 200
