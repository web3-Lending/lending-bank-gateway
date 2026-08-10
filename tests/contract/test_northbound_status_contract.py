"""北向状态字面量契约测试：锁死上游 lending-lifecycel 依赖的 txnStatus 取值。

**为什么需要这层测试**：上游 lending-lifecycel 是只读仓（本方无权改），它对还款失败的
处置**完全由 gateway 北向返回的 txnStatus 字符串决定**，且是字面量匹配：

    app_flow_loans.py:1118   if repay_result.error_code == "RESULT_UNKNOWN":
                                 → 写 repaymentResultUnknown marker，挂起、**不回滚**
                             else:
    app_flow_loans.py:1151       app_row.status = prev_status  # **回滚本地状态**

即：gateway 这边一旦把 ``OrderStatus.RESULT_UNKNOWN`` 改名、或北向响应改用别的字段/取值，
上游会**静默**从「挂起等收敛」掉进「回滚」分支——而此时 wedap 侧资金可能已经变动，
本地回滚即产生状态撕裂型资金错账。跨仓没有编译期约束，也没有共享类型，只有这条测试。

同理 ``"SUCCESS"``/``"PROCESSING"`` 也被上游 _TXN_SUCCESS_STATUSES / _TXN_PROCESSING_STATUSES
按字面量匹配（bank_p2p.py:385-386），一并钉死。

改这些字面量 = 改跨仓契约，必须先同步 lending-lifecycel owner，不是本仓能单方面决定的事。
"""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.clients.wedap import WedapError
from app.core.db import build_engine, build_session_factory
from app.domain.states import OrderStatus
from app.models.base import Base
from app.models.txn import BankTxnOrder
from app.services.submit import SubmitRequest, submit_order

# 上游读的是**北向 txnStatus**，其值域来自 wedap 契约（SUCCESS/PROCESSING/FAILED/...），
# 与 gateway 台账的 OrderStatus 是**两套不同的取值空间**——只有 RESULT_UNKNOWN / FAILED
# 两个字符串在两边同形。早先把 OrderStatus.SUCCEEDED→"SUCCEEDED" 也写进来是空守卫：
# "SUCCEEDED" 只会出现在 orderStatus 字段，上游根本不读（2026-08-10 独立评审 finding）。
#
# 上游 bank_p2p.py:385-386 / app_flow_loans.py:1118 按字面量匹配的 txnStatus 取值：
UPSTREAM_MATCHED_TXN_STATUS = {
    "SUCCESS": "落账 → 上游核销债务",
    "PROCESSING": "在途 → 上游 is_ok=False 且**会回滚**（见 FU-LIFECYCEL-REPAY-UNKNOWN-RESOLVER）",
    "FAILED": "确证拒绝 → 上游回滚（此时零资金变动，安全）",
    "RESULT_UNKNOWN": "唯一让上游挂起且不回滚的值",
}
# gateway 台账枚举中被上游同名字面量依赖的成员（仅此二者跨两套空间同形）
ORDER_STATUS_SHARED_LITERALS = {
    OrderStatus.RESULT_UNKNOWN: "RESULT_UNKNOWN",
    OrderStatus.FAILED: "FAILED",
}


@pytest.fixture()
async def factory():
    engine = build_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield build_session_factory(engine)
    await engine.dispose()


def _repay_req(**kw) -> SubmitRequest:
    d = dict(
        tenant_id="OCBC",
        biz_seq_no="RPMT-20260810-contract-0001",
        business_action="REPAY",
        biz_type="RPMT",
        amount=Decimal("100.0000"),
        currency="USD",
        caller_service="lifecycle",
        request_id="req-contract-1",
        business_scope="p2p_repay",
        wedap_payload={"bizSeqNo": "RPMT-20260810-contract-0001"},
        repayment_contract=True,
    )
    d.update(kw)
    return SubmitRequest(**d)


def test_order_status_enum_values_are_upstream_contract() -> None:
    """OrderStatus 中与北向 txnStatus 同形的成员，其字符串值即跨仓契约。"""
    for member, literal in ORDER_STATUS_SHARED_LITERALS.items():
        assert str(member) == literal, (
            f"OrderStatus.{member.name} 的字符串值被改成 {str(member)!r}，"
            f"但上游 lending-lifecycel 按 {literal!r} 字面量匹配——改名前必须同步该仓 owner"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("wedap_status", "expected_northbound"),
    [
        ("success", "SUCCESS"),  # 大小写偏差必须归一化，否则上游认不出 → 回滚
        ("Success", "SUCCESS"),
        (" SUCCESS ", "SUCCESS"),
        ("processing", "PROCESSING"),
        ("failed", "FAILED"),
        ("REVERSED", "RESULT_UNKNOWN"),  # 还款契约无此值 → 落兜底档
        ("WEIRD_NEW_VALUE", "RESULT_UNKNOWN"),  # wedap 新增未知值 → 保守挂起
        ("", "RESULT_UNKNOWN"),
    ],
)
async def test_northbound_txn_status_is_normalized_closed_set(
    factory, wedap_status: str, expected_northbound: str
) -> None:
    """北向 txnStatus 必须归一化到封闭值域，绝不原样透传 wedap 字符串。

    真实失败场景（2026-08-10 独立评审实测）：wedap 返 {"status":"success"}，gateway 内部
    map_wedap_repayment_status 有 .upper() 故台账正确落 SUCCEEDED 并转发 loanRepaid，
    但北向若原样输出 "success"，上游 _TXN_SUCCESS_STATUSES={"SUCCESS"} 匹配不上 →
    error_code="success" ≠ "RESULT_UNKNOWN" → app_flow_loans.py:1151 回滚本地状态。
    台账说成功、上游在回滚 = 状态撕裂。
    """
    wedap = AsyncMock()
    wedap.submit_repayment.return_value = {"status": wedap_status, "debtSettled": False}
    result = await submit_order(
        factory,
        wedap_call=wedap.submit_repayment,
        req=_repay_req(biz_seq_no=f"RPMT-norm-{abs(hash(wedap_status)) % 10**10}"),
    )
    assert result["txnStatus"] == expected_northbound
    assert result["txnStatus"] in UPSTREAM_MATCHED_TXN_STATUS, (
        f"北向 txnStatus 输出了 {result['txnStatus']!r}，不在上游能识别的封闭值域内——"
        f"上游按字面量匹配，认不出的值会让它走回滚分支"
    )


@pytest.mark.asyncio
async def test_generic_txn_status_also_normalized(factory) -> None:
    """通用交易（放款/归集/分发/退款/冲正）走同一条归一化规则，不因分支不同而漏。"""
    wedap = AsyncMock()
    wedap.submit_disbursement.return_value = {"txnStatus": "success"}
    result = await submit_order(
        factory,
        wedap_call=wedap.submit_disbursement,
        req=_repay_req(
            biz_seq_no="DSB-norm-0001",
            business_action="DISBURSE",
            biz_type="DISB",
            business_scope="p2p_disburse",
            repayment_contract=False,
        ),
    )
    assert result["txnStatus"] == "SUCCESS"
    assert result["orderStatus"] == OrderStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_pending_business_code_yields_upstream_hold_literal(factory) -> None:
    """待轮询业务码 → 北向 txnStatus 必须恰好是 "RESULT_UNKNOWN"。

    这是上游走「挂起不回滚」分支的**唯一**触发条件。返回任何其它取值（含 "PROCESSING"）
    都会让上游回滚本地状态——而此时 wedap 可能已扣款。
    """
    wedap = AsyncMock()
    wedap.submit_repayment.side_effect = WedapError("6605U00900211", "交易结果待确认")
    result = await submit_order(factory, wedap_call=wedap.submit_repayment, req=_repay_req())

    assert result["txnStatus"] == "RESULT_UNKNOWN"
    # 本地台账同步为非终态，兜底 worker 才会继续收敛（_NON_TERMINAL 含该态）
    async with factory() as s:
        order = (await s.execute(select(BankTxnOrder))).scalar_one()
        assert order.status == OrderStatus.RESULT_UNKNOWN


@pytest.mark.asyncio
async def test_sync_success_yields_upstream_success_literal(factory) -> None:
    """还款同步落账 → 北向 txnStatus 必须是 "SUCCESS"（上游据此核销债务）。"""
    wedap = AsyncMock()
    wedap.submit_repayment.return_value = {
        "status": "SUCCESS",
        "detailStatus": "SUCCESS",
        "debtSettled": True,
    }
    result = await submit_order(
        factory,
        wedap_call=wedap.submit_repayment,
        req=_repay_req(biz_seq_no="RPMT-20260810-contract-0002"),
    )
    assert result["txnStatus"] == "SUCCESS"
    assert result["debtSettled"] is True


@pytest.mark.asyncio
async def test_confirmed_reject_yields_upstream_failed_literal(factory) -> None:
    """确证拒绝码 → 北向 txnStatus 必须是 "FAILED"（上游据此回滚，此时零资金变动）。"""
    wedap = AsyncMock()
    wedap.submit_repayment.side_effect = WedapError("6605B00900205", "账户余额不足")
    result = await submit_order(
        factory,
        wedap_call=wedap.submit_repayment,
        req=_repay_req(biz_seq_no="RPMT-20260810-contract-0003"),
    )
    assert result["txnStatus"] == "FAILED"
