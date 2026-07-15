"""北向退款 API 测试：POST /api/v1/bank-funds/refunds（S5.6 拍板 2026-07-14）。"""

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from app.models.base import Base
from app.models.txn import BankTxnOrder

BIZ = "RFD-20260714-0001234567890"
HEADERS = {
    "X-Caller-Service": "lifecycle",
    "X-Tenant-Id": "OCBC",
    "X-Request-Id": "req-rfd-1",
    "Idempotency-Key": BIZ,
}
REFUND_BODY = {
    # 对接文档 v0.3.0 §4.7 形态：oriBizSeqNo 关联原归集单，其余 wedap 字段 extra=allow 透传
    "bizSeqNo": BIZ,
    "channelId": "LEN",
    "transType": "REFUND",
    "oriBizSeqNo": "CLT-20260714-0001234567890",
    "refundAmount": "0.30",
    "currencyCode": "USD",
    "bankAccountNo": "9999617459809900000215",
    "custAccountNo": "1001234567890",
    "subaccountSerialNo": "00000001",
}


async def _create_tables(engine) -> None:  # type: ignore[no-untyped-def]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.fixture()
def client() -> TestClient:
    app = create_app()
    asyncio.run(_create_tables(app.state.engine))
    wedap = AsyncMock()
    wedap.refund.return_value = {
        "txnStatus": "SUCCESS",
        "bizSeqNo": BIZ,
        "refundedTotalAmount": "0.30",
    }
    app.state.wedap = wedap
    return TestClient(app)


def _order(client: TestClient) -> BankTxnOrder:
    async def _get() -> BankTxnOrder:
        async with client.app.state.session_factory() as s:  # type: ignore[union-attr]
            return (
                await s.execute(select(BankTxnOrder).where(BankTxnOrder.biz_seq_no == BIZ))
            ).scalar_one()

    return asyncio.run(_get())


def test_refund_success_lands_rfnd_order(client: TestClient) -> None:
    """SUCCESS → 200 + 台账落 RFND 单（SUCCEEDED/SYNC），trans_type/ori_req_date 供参就位。"""
    r = client.post("/api/v1/bank-funds/refunds", json=REFUND_BODY, headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["data"]["txnStatus"] == "SUCCESS"
    assert body["data"]["bizSeqNo"] == BIZ
    order = _order(client)
    assert order.biz_type == "RFND"
    assert order.business_action == "REFUND"
    assert order.status == "SUCCEEDED"
    assert order.amount == Decimal("0.30")
    assert order.trans_type == "REFUND"
    assert order.ori_req_date is not None and len(order.ori_req_date) == 8
    # 薄透传：wedap 收到的 payload 含 oriBizSeqNo 与行内格式内部户字段
    payload = client.app.state.wedap.refund.call_args.kwargs["payload"]  # type: ignore[union-attr]
    assert payload["oriBizSeqNo"] == REFUND_BODY["oriBizSeqNo"]
    assert payload["bankAccountNo"] == REFUND_BODY["bankAccountNo"]


def test_refund_idempotent_replay_returns_first_response(client: TestClient) -> None:
    """同键同 payload 重放 → 返回首个响应，wedap 只被调一次（零重复退款）。"""
    r1 = client.post("/api/v1/bank-funds/refunds", json=REFUND_BODY, headers=HEADERS)
    r2 = client.post("/api/v1/bank-funds/refunds", json=REFUND_BODY, headers=HEADERS)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.json()["data"]["txnStatus"] == "SUCCESS"
    assert client.app.state.wedap.refund.await_count == 1  # type: ignore[union-attr]


def test_refund_conflict_same_biz_seq_no_different_amount_409(client: TestClient) -> None:
    """同 bizSeqNo 改金额 → 409 GW_409_IDEMPOTENCY，拒绝不覆盖首单。"""
    client.post("/api/v1/bank-funds/refunds", json=REFUND_BODY, headers=HEADERS)
    r = client.post(
        "/api/v1/bank-funds/refunds",
        json={**REFUND_BODY, "refundAmount": "0.40"},
        headers=HEADERS,
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "GW_409_IDEMPOTENCY"


def test_refund_missing_ori_biz_seq_no_422(client: TestClient) -> None:
    """缺 oriBizSeqNo（pydantic 必填）→ 422，不落单不外呼。"""
    body = {k: v for k, v in REFUND_BODY.items() if k != "oriBizSeqNo"}
    r = client.post("/api/v1/bank-funds/refunds", json=body, headers=HEADERS)
    assert r.status_code == 422
    client.app.state.wedap.refund.assert_not_called()  # type: ignore[union-attr]


def test_refund_bad_amount_400(client: TestClient) -> None:
    """非法金额（负数）→ 400 GW_400_VALIDATION，不外呼。"""
    r = client.post(
        "/api/v1/bank-funds/refunds",
        json={**REFUND_BODY, "refundAmount": "-1.00"},
        headers=HEADERS,
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "GW_400_VALIDATION"
    client.app.state.wedap.refund.assert_not_called()  # type: ignore[union-attr]


def test_refund_wedap_business_failure_wraps_200_failed(client: TestClient) -> None:
    """wedap 返回业务 FAILED（如超额退款 422 包装）→ 200 + txnStatus=FAILED 落单。"""
    client.app.state.wedap.refund.return_value = {"txnStatus": "FAILED"}  # type: ignore[union-attr]
    r = client.post("/api/v1/bank-funds/refunds", json=REFUND_BODY, headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["data"]["txnStatus"] == "FAILED"
    order = _order(client)
    assert order.status == "FAILED"


def test_refund_full_amount_guard_off_by_default(client: TestClient) -> None:
    """flag 默认关：全额退款（== 原单金额）不被 gateway 拦，照常提交 wedap（过渡口径）。"""
    # 先种一笔归集原单
    coll = {
        "bizSeqNo": "CLT-20260715-GUARD-OFF-01",
        "transType": "LOAN_COLLECT",
        "txnAmount": "1.00",
        "currencyCode": "USD",
    }
    client.app.state.wedap.collect_from_users = AsyncMock(  # type: ignore[union-attr]
        return_value={"txnStatus": "SUCCESS"}
    )
    h = {**HEADERS, "Idempotency-Key": coll["bizSeqNo"], "X-Request-Id": "req-goff-c"}
    client.post("/api/v1/bank-funds/collect-from-users", json=coll, headers=h)
    body = {
        **REFUND_BODY,
        "bizSeqNo": "RFD-20260715-GUARD-OFF-01",
        "oriBizSeqNo": coll["bizSeqNo"],
        "refundAmount": "1.00",  # == 原单金额（全额）
    }
    h = {**HEADERS, "Idempotency-Key": body["bizSeqNo"], "X-Request-Id": "req-goff-r"}
    r = client.post("/api/v1/bank-funds/refunds", json=body, headers=h)
    assert r.status_code == 200
    assert r.json()["data"]["txnStatus"] == "SUCCESS"


def test_refund_full_amount_guard_on_rejects_and_partial_passes(client: TestClient) -> None:
    """flag 开：全额退款 422 导流冲正（GW_422_FULL_REFUND_USE_REVERSAL）；部分退款放行；
    原单不在本地台账 → 不拦（交 wedap 校验）。"""
    coll = {
        "bizSeqNo": "CLT-20260715-GUARD-ON-01",
        "transType": "LOAN_COLLECT",
        "txnAmount": "2.00",
        "currencyCode": "USD",
    }
    client.app.state.wedap.collect_from_users = AsyncMock(  # type: ignore[union-attr]
        return_value={"txnStatus": "SUCCESS"}
    )
    h = {**HEADERS, "Idempotency-Key": coll["bizSeqNo"], "X-Request-Id": "req-gon-c"}
    client.post("/api/v1/bank-funds/collect-from-users", json=coll, headers=h)

    client.app.state.settings.refund_full_amount_guard = True
    try:
        # 全额 → 422 导流冲正，不外呼
        body = {
            **REFUND_BODY,
            "bizSeqNo": "RFD-20260715-GUARD-ON-01",
            "oriBizSeqNo": coll["bizSeqNo"],
            "refundAmount": "2.00",
        }
        h = {**HEADERS, "Idempotency-Key": body["bizSeqNo"], "X-Request-Id": "req-gon-r1"}
        r = client.post("/api/v1/bank-funds/refunds", json=body, headers=h)
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "GW_422_FULL_REFUND_USE_REVERSAL"
        client.app.state.wedap.refund.assert_not_called()  # type: ignore[union-attr]
        # 部分（1.50 < 2.00）→ 放行
        body2 = {
            **REFUND_BODY,
            "bizSeqNo": "RFD-20260715-GUARD-ON-02",
            "oriBizSeqNo": coll["bizSeqNo"],
            "refundAmount": "1.50",
        }
        h = {**HEADERS, "Idempotency-Key": body2["bizSeqNo"], "X-Request-Id": "req-gon-r2"}
        r2 = client.post("/api/v1/bank-funds/refunds", json=body2, headers=h)
        assert r2.status_code == 200
        # 原单不在台账（外部单号）→ 不拦
        body3 = {
            **REFUND_BODY,
            "bizSeqNo": "RFD-20260715-GUARD-ON-03",
            "oriBizSeqNo": "EXT-UNKNOWN-ORI-01",
            "refundAmount": "9.99",
        }
        h = {**HEADERS, "Idempotency-Key": body3["bizSeqNo"], "X-Request-Id": "req-gon-r3"}
        r3 = client.post("/api/v1/bank-funds/refunds", json=body3, headers=h)
        assert r3.status_code == 200
    finally:
        client.app.state.settings.refund_full_amount_guard = False
