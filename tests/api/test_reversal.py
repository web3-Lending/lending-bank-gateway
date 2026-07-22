"""北向通用冲正 API 测试：POST /api/v1/bank-funds/reversals（对接 wedap Public.md §4.4.2）。"""

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.domain.states import OrderStatus
from app.main import create_app
from app.models.base import Base
from app.models.txn import BankTxnOrder

RVSL = "RVSL-20260722-0001234567890"
COLL = "CLT-20260722-0001234567890"
HEADERS = {
    "X-Caller-Service": "liquidation",
    "X-Tenant-Id": "WBTHK01",
    "X-Request-Id": "req-rvsl-1",
    "Idempotency-Key": RVSL,
}
REVERSAL_BODY = {
    "bizSeqNo": RVSL,
    "channelId": "W3C",
    "transType": "LOAN_COLLECT",
    "oriBizSeqNo": COLL,
    "oriReqDate": "20260722",
    "oriTxnAmount": "5000.0000",
    "currencyCode": "USD",
    "reason": "abort 全额冲正",
}


async def _create_tables(engine) -> None:  # type: ignore[no-untyped-def]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed_collect(factory) -> None:  # type: ignore[no-untyped-def]
    async with factory() as s:
        async with s.begin():
            s.add(
                BankTxnOrder(
                    tenant_id="WBTHK01",
                    biz_seq_no=COLL,
                    business_action="COLLECT",
                    biz_type="COLL",
                    amount=Decimal("5000.0000"),
                    currency="USD",
                    caller_service="liquidation",
                    status=OrderStatus.SUCCEEDED,
                    request_id="req-coll",
                    trans_type="LOAN_COLLECT",
                )
            )


@pytest.fixture()
def client() -> TestClient:
    app = create_app()
    asyncio.run(_create_tables(app.state.engine))
    asyncio.run(_seed_collect(app.state.session_factory))
    wedap = AsyncMock()
    wedap.reverse.return_value = {
        "txnStatus": "REVERSED",
        "bizSeqNo": RVSL,
        "reversalBizSeqNo": "R-1",
    }
    app.state.wedap = wedap
    return TestClient(app)


def test_reversal_succeeds_and_flips_original(client: TestClient) -> None:
    r = client.post("/api/v1/bank-funds/reversals", json=REVERSAL_BODY, headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["data"]["txnStatus"] == "REVERSED"

    async def _check() -> tuple[str, str]:
        async with client.app.state.session_factory() as s:  # type: ignore[union-attr]
            rvsl = (
                await s.execute(select(BankTxnOrder).where(BankTxnOrder.biz_seq_no == RVSL))
            ).scalar_one()
            ori = (
                await s.execute(select(BankTxnOrder).where(BankTxnOrder.biz_seq_no == COLL))
            ).scalar_one()
            return rvsl.status, ori.status

    rvsl_status, ori_status = asyncio.run(_check())
    assert rvsl_status == OrderStatus.SUCCEEDED
    assert ori_status == OrderStatus.REVERSED


def test_reversal_passes_original_transtype_to_wedap(client: TestClient) -> None:
    client.post("/api/v1/bank-funds/reversals", json=REVERSAL_BODY, headers=HEADERS)
    _, kwargs = client.app.state.wedap.reverse.call_args  # type: ignore[union-attr]
    assert kwargs["payload"]["transType"] == "LOAN_COLLECT"
    assert kwargs["payload"]["oriBizSeqNo"] == COLL


def test_reversal_missing_ori_biz_seq_no_422(client: TestClient) -> None:
    body = {k: v for k, v in REVERSAL_BODY.items() if k != "oriBizSeqNo"}
    r = client.post("/api/v1/bank-funds/reversals", json=body, headers=HEADERS)
    assert r.status_code == 422


def test_reversal_bad_biz_seq_no_400(client: TestClient) -> None:
    """bizSeqNo 格式不合规 → submit_reversal 内 ValueError → 400 GW_400_VALIDATION。"""
    bad_key = "WB-1704067200000-REVERSAL-10-0001-123456"
    body_bad = {**REVERSAL_BODY, "bizSeqNo": bad_key}
    h = {**HEADERS, "Idempotency-Key": bad_key}
    r = client.post("/api/v1/bank-funds/reversals", json=body_bad, headers=h)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "GW_400_VALIDATION"


def test_reversal_same_key_different_payload_409(client: TestClient) -> None:
    """同 Idempotency-Key 不同 payload（金额变化）→ payload hash 变化 → 409 GW_409_IDEMPOTENCY。"""
    client.post("/api/v1/bank-funds/reversals", json=REVERSAL_BODY, headers=HEADERS)
    mutated = {**REVERSAL_BODY, "oriTxnAmount": "999.0000"}
    r = client.post("/api/v1/bank-funds/reversals", json=mutated, headers=HEADERS)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "GW_409_IDEMPOTENCY"
