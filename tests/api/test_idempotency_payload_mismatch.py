"""API-HTTP-019 / v2.2 §9.1：同幂等键 + 不同 payload 的北向状态码与「不得 dispatch」。

规范 §9.1（判据 `00-lending-api-design-spec-v2.2.md:672`）对高风险写请求规定：

    同键、不同 payload：返回 HTTP `422`，Problem
    `type=https://api.wts.com/problems/idempotency-payload-mismatch/v1`；
    调用方必须停止重放并先查询原 operation。……服务端不得 dispatch。

§6 逐规则表把 API-HTTP-019 的「例外审批人 / 例外到期」两列写成**不允许 / 不适用**，
故本条没有限期例外可申请——码必须是 422，不能停在本仓自选的 409。

本文件覆盖全部 **6 个 MONEY_WRITE 写原语**（不是抽一个代表）：漏掉任何一个，
那条资金通路上的「换了金额重提」就仍旧拿不到规范要求的可编程信号。

另有一条**全字段指纹**用例：把变异点放在离金额最远的字段（收款银行账号 / 冲正原因）。
指纹一旦被收窄成「只看金额」，钱数没变但**收款人变了**的重提会被当成重复请求放过去——
这正是本条要堵的失败方向，故它必须有独立用例守着，不能只靠金额用例。
"""

import asyncio
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.domain.states import OrderStatus
from app.main import create_app
from app.models.base import Base
from app.models.txn import BankTxnOrder

TENANT = "OCBC"

COLLECT_BIZ = "CLT-20260830-0001234567890"
DISTRIBUTE_BIZ = "DST-20260830-0001234567890"
REFUND_BIZ = "RFD-20260830-0001234567890"
REVERSAL_BIZ = "RVSL-20260830-0001234567890"
DISBURSE_BIZ = "DSB-20260830-0001234567890"
REPAY_BIZ = "RPMT-20260830-0001234567890"
#: 被冲正 / 被退款的原归集单，由 fixture 预先落台账。
ORIGIN_COLLECT_BIZ = "CLT-20260830-0009999999999"

COLLECT_BODY: dict[str, Any] = {
    "bizSeqNo": COLLECT_BIZ,
    "transType": "LOAN_COLLECT",
    "txnAmount": "500.0000",
    "currencyCode": "USD",
    "channelId": "LEN",
    "userId": "U1",
    "custAccountNo": "1001234567890",
    "bankAccountNo": "9999617459809900000215",
    "bankAccountName": "TEST COLLECTION ACCOUNT",
    "userList": [{"userId": "U1", "amount": "500.0000"}],
}
DISTRIBUTE_BODY: dict[str, Any] = {
    "bizSeqNo": DISTRIBUTE_BIZ,
    "transType": "BANK_FUND_DISTRIBUTE",
    "currencyCode": "USD",
    "channelId": "LEN",
    "bankAccountNo": "9999617459809900000215",
    "bankAccountName": "TEST PLATFORM ACCOUNT",
    "recipients": [
        {
            "userId": "U2",
            "userName": "TEST RECIPIENT",
            "custAccountNo": "1001234567891",
            "distributeAmount": "200.0000",
            "currencyCode": "USD",
        }
    ],
}
REFUND_BODY: dict[str, Any] = {
    "bizSeqNo": REFUND_BIZ,
    "channelId": "LEN",
    "transType": "REFUND",
    "oriBizSeqNo": ORIGIN_COLLECT_BIZ,
    # 原单 5000，退 0.30 → 部分退款，绕开 GW_422_FULL_REFUND_USE_REVERSAL 全额护栏
    "refundAmount": "0.30",
    "currencyCode": "USD",
    "bankAccountNo": "9999617459809900000215",
    "custAccountNo": "1001234567890",
    "subaccountSerialNo": "00000001",
}
REVERSAL_BODY: dict[str, Any] = {
    "bizSeqNo": REVERSAL_BIZ,
    "channelId": "W3C",
    "transType": "LOAN_COLLECT",
    "oriBizSeqNo": ORIGIN_COLLECT_BIZ,
    "oriReqDate": "20260830",
    "oriTxnAmount": "5000.0000",
    "currencyCode": "USD",
    "reason": "abort full reversal",
}
DISBURSE_BODY: dict[str, Any] = {
    "bizSeqNo": DISBURSE_BIZ,
    "channelId": "LEN",
    "transType": "DISBURSEMENT",
    "disbursementInfo": {
        "txnAmount": "100.0000",
        "currencyCode": "USD",
        "userId": "U1",
        "userName": "u",
    },
    "lenders": [{"userId": "L1", "lendAmount": "100.0000", "currencyCode": "USD"}],
}
REPAY_BODY: dict[str, Any] = {
    "bizSeqNo": REPAY_BIZ,
    "transType": "REPAYMENT",
    "channelId": "LEN",
    "loanNo": "LN-0001",
    "repaymentInfo": {
        "txnAmount": "100.0000",
        "currencyCode": "USD",
        "userId": "U1",
        "userName": "u",
        "repaymentType": "NORMAL",
        "principalAmount": "90.0000",
        "interestAmount": "10.0000",
    },
    "lenders": [
        {
            "userId": "L1",
            "txnAmount": "100.0000",
            "currencyCode": "USD",
            "principalAmount": "90.0000",
            "interestAmount": "10.0000",
        }
    ],
}


def _headers(biz_seq_no: str) -> dict[str, str]:
    return {
        "X-Caller-Service": "lifecycle",
        "X-Tenant-Id": TENANT,
        "X-Request-Id": f"req-{biz_seq_no}",
        "Idempotency-Key": biz_seq_no,
    }


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


async def _create_tables(engine) -> None:  # type: ignore[no-untyped-def]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed_origin_collect(factory) -> None:  # type: ignore[no-untyped-def]
    """退款 / 冲正的原归集单：5000 已成功，供 oriBizSeqNo 关联。"""
    async with factory() as session:
        async with session.begin():
            session.add(
                BankTxnOrder(
                    tenant_id=TENANT,
                    biz_seq_no=ORIGIN_COLLECT_BIZ,
                    business_action="COLLECT",
                    biz_type="COLL",
                    amount=Decimal("5000.0000"),
                    currency="USD",
                    caller_service="lifecycle",
                    status=OrderStatus.SUCCEEDED,
                    request_id="req-origin",
                    trans_type="LOAN_COLLECT",
                )
            )


@pytest.fixture()
def client() -> TestClient:
    app = create_app()
    asyncio.run(_create_tables(app.state.engine))
    asyncio.run(_seed_origin_collect(app.state.session_factory))
    wedap = AsyncMock()
    for attr in (
        "collect_from_users",
        "distribute_to_users",
        "refund",
        "submit_disbursement",
    ):
        getattr(wedap, attr).return_value = {"txnStatus": "PROCESSING"}
    wedap.reverse.return_value = {
        "txnStatus": "REVERSED",
        "bizSeqNo": REVERSAL_BIZ,
        "reversalBizSeqNo": "R-1",
    }
    # 还款受理响应按 DTC 契约（v0.6.1 §4.2）：status 而非 txnStatus。
    wedap.submit_repayment.return_value = {
        "bizSeqNo": REPAY_BIZ,
        "globalTxId": "GT20260830000001",
        "status": "PROCESSING",
        "detailStatus": "PROCESSING",
        "debtSettled": False,
    }
    app.state.wedap = wedap
    return TestClient(app)


def _order_count(client: TestClient) -> int:
    async def _count() -> int:
        async with client.app.state.session_factory() as session:  # type: ignore[union-attr]
            return int((await session.execute(select(func.count(BankTxnOrder.id)))).scalar_one())

    return asyncio.run(_count())


#: (用例名, 路径, 首次报文, wedap 方法名, 金额变异, 非金额变异)
_MONEY_WRITES: list[tuple[str, str, dict[str, Any], str, dict[str, Any], dict[str, Any]]] = [
    (
        "collect",
        "/api/v1/bank-funds/collect-from-users",
        COLLECT_BODY,
        "collect_from_users",
        {"txnAmount": "999.0000", "userList": [{"userId": "U1", "amount": "999.0000"}]},
        {"bankAccountNo": "9999617459809900000999"},
    ),
    (
        "distribute",
        "/api/v1/bank-funds/distribute-to-users",
        DISTRIBUTE_BODY,
        "distribute_to_users",
        {
            "recipients": [
                {**DISTRIBUTE_BODY["recipients"][0], "distributeAmount": "999.0000"},
            ]
        },
        {"bankAccountNo": "9999617459809900000999"},
    ),
    (
        "refund",
        "/api/v1/bank-funds/refunds",
        REFUND_BODY,
        "refund",
        {"refundAmount": "0.40"},
        {"custAccountNo": "1009999999999"},
    ),
    (
        "reversal",
        "/api/v1/bank-funds/reversals",
        REVERSAL_BODY,
        "reverse",
        {"oriTxnAmount": "4000.0000"},
        {"reason": "operator changed the stated reason"},
    ),
    (
        "disbursement",
        "/api/v1/loans/p2p-disbursements",
        DISBURSE_BODY,
        "submit_disbursement",
        {
            "disbursementInfo": {"txnAmount": "999.0000"},
            "lenders": [{"userId": "L1", "lendAmount": "999.0000", "currencyCode": "USD"}],
        },
        {"lenders": [{**DISBURSE_BODY["lenders"][0], "userId": "L9"}]},
    ),
    (
        "repayment",
        "/api/v1/loans/p2p-repayments",
        REPAY_BODY,
        "submit_repayment",
        {
            "repaymentInfo": {"txnAmount": "999.0000", "principalAmount": "989.0000"},
            "lenders": [{**REPAY_BODY["lenders"][0], "txnAmount": "999.0000"}],
        },
        {"loanNo": "LN-9999"},
    ),
]

_AMOUNT_CASES = [
    (name, path, body, attr, patch) for name, path, body, attr, patch, _ in _MONEY_WRITES
]
_NON_AMOUNT_CASES = [
    (name, path, body, attr, patch) for name, path, body, attr, _, patch in _MONEY_WRITES
]


@pytest.mark.parametrize(
    ("path", "body", "wedap_attr", "patch"),
    [case[1:] for case in _AMOUNT_CASES],
    ids=[case[0] for case in _AMOUNT_CASES],
)
def test_same_key_different_amount_is_422_and_not_dispatched(
    client: TestClient,
    path: str,
    body: dict[str, Any],
    wedap_attr: str,
    patch: dict[str, Any],
) -> None:
    """§9.1：同键 + 换了金额 → 422 + 不得 dispatch（不是回放旧结果、也不是本仓自选的 409）。"""
    headers = _headers(body["bizSeqNo"])
    first = client.post(path, json=body, headers=headers)
    assert first.status_code == 200, first.json()
    call = getattr(client.app.state.wedap, wedap_attr)  # type: ignore[union-attr]
    assert call.await_count == 1
    orders_before = _order_count(client)

    second = client.post(path, json=_deep_merge(body, patch), headers=headers)

    assert second.status_code == 422, second.json()
    error = second.json()["error"]
    assert error["code"] == "GW_422_IDEMPOTENCY_PAYLOAD_MISMATCH"
    # §9.1「服务端不得 dispatch」：既不外呼，也不新建台账单。
    assert call.await_count == 1
    assert _order_count(client) == orders_before


@pytest.mark.parametrize(
    ("path", "body", "wedap_attr", "patch"),
    [case[1:] for case in _NON_AMOUNT_CASES],
    ids=[case[0] for case in _NON_AMOUNT_CASES],
)
def test_same_key_non_amount_field_change_is_also_422(
    client: TestClient,
    path: str,
    body: dict[str, Any],
    wedap_attr: str,
    patch: dict[str, Any],
) -> None:
    """全字段指纹：金额没动、别的字段动了（收款账号 / 出借人 / 借据号）同样 422。

    把指纹收窄成「只比金额」会让「钱数不变但收款人变了」的重提静默回放旧结果——
    钱进了别人的账户而调用方以为成功。故非金额字段必须同样被指纹覆盖。
    """
    headers = _headers(body["bizSeqNo"])
    assert client.post(path, json=body, headers=headers).status_code == 200
    call = getattr(client.app.state.wedap, wedap_attr)  # type: ignore[union-attr]

    second = client.post(path, json=_deep_merge(body, patch), headers=headers)

    assert second.status_code == 422, second.json()
    assert second.json()["error"]["code"] == "GW_422_IDEMPOTENCY_PAYLOAD_MISMATCH"
    assert call.await_count == 1


def test_same_key_same_payload_still_replays_the_frozen_response(client: TestClient) -> None:
    """反向守卫：真正的重复请求（同键同 payload）仍是 200 重放，不得被本条改成 422。

    没有这条，把比对写成「第二次一律 422」也能让上面全绿——那会让所有正常重试全断。
    """
    headers = _headers(COLLECT_BIZ)
    first = client.post("/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=headers)
    second = client.post(
        "/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=headers
    )
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["data"] == second.json()["data"]
    assert client.app.state.wedap.collect_from_users.await_count == 1  # type: ignore[union-attr]
