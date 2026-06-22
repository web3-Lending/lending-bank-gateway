"""同步优先终态收口 V2 新行为测试：submit CAS/映射、order_finalize 幂等、order_reconcile worker。"""

import datetime as dt
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.core.db import build_engine, build_session_factory
from app.domain.states import OrderStatus
from app.models.audit import AuditLog
from app.models.base import Base
from app.models.callback import CallbackOutbox
from app.models.txn import BankTxnLeg, BankTxnOrder
from app.services.legs import LegsSyncIncomplete
from app.services.order_finalize import finalize_terminal_in_session
from app.services.submit import SubmitRequest, submit_order
from app.workers.order_reconcile_worker import reconcile_once


@pytest.fixture()
async def factory():
    engine = build_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield build_session_factory(engine)
    await engine.dispose()


def _req(**kw) -> SubmitRequest:
    d = dict(
        tenant_id="OCBC",
        biz_seq_no="DSB-20260611-0001234567890",
        business_action="DISBURSE",
        biz_type="DSB",
        amount=Decimal("100.0000"),
        currency="USD",
        caller_service="lifecycle",
        request_id="req-1",
        business_scope="p2p_disburse",
        wedap_payload={"bizSeqNo": "DSB-20260611-0001234567890"},
    )
    d.update(kw)
    return SubmitRequest(**d)


# ── submit 同步终态映射 + CAS ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_success_sets_succeeded_and_forwards(factory) -> None:
    """wedap 同步 SUCCESS → order SUCCEEDED + finalized_at/via=SYNC + 转发 outbox。"""
    wedap = AsyncMock()
    wedap.submit_disbursement.return_value = {"txnStatus": "SUCCESS"}
    result = await submit_order(factory, wedap_call=wedap.submit_disbursement, req=_req())
    assert result["txnStatus"] == "SUCCESS"
    async with factory() as s:
        order = (await s.execute(select(BankTxnOrder))).scalar_one()
        assert order.status == OrderStatus.SUCCEEDED
        assert order.finalized_at is not None
        assert order.finalized_via == "SYNC"
        outbox = (await s.execute(select(CallbackOutbox))).scalar_one()
        assert outbox.target == "lifecycle"
        assert outbox.dedup_key == "fwd-OCBC-DSB-20260611-0001234567890-SUCCEEDED"
        audit = (await s.execute(select(AuditLog))).scalars().all()
        assert any(a.action == "ORDER_SUCCEEDED" for a in audit)


@pytest.mark.asyncio
async def test_sync_http200_business_failed_sets_failed_and_forwards(factory) -> None:
    """wedap HTTP 200 但 txnStatus=FAILED（业务失败）→ order FAILED + 终态收口转发。"""
    wedap = AsyncMock()
    wedap.submit_disbursement.return_value = {"txnStatus": "FAILED"}
    await submit_order(factory, wedap_call=wedap.submit_disbursement, req=_req())
    async with factory() as s:
        order = (await s.execute(select(BankTxnOrder))).scalar_one()
        assert order.status == OrderStatus.FAILED
        assert order.finalized_via == "SYNC"
        assert (await s.execute(select(CallbackOutbox))).scalar_one().target == "lifecycle"


@pytest.mark.asyncio
async def test_cas_skips_when_callback_already_advanced(factory) -> None:
    """tx1/tx2 之间回调已推进 SUCCEEDED → tx2 CAS（WHERE status=ACCEPTED）skip 不覆盖。"""

    async def _wedap_call(**kw):
        # 模拟并发回调：在外呼（tx1 后 tx2 前）已聚合终态 + 收口
        async with factory() as s:
            async with s.begin():
                order = (await s.execute(select(BankTxnOrder))).scalar_one()
                order.status = OrderStatus.SUCCEEDED
                order.finalized_at = dt.datetime.now(dt.UTC)
                order.finalized_via = "CALLBACK"
        return {"txnStatus": "PROCESSING"}  # 本次外呼返回 PROCESSING（弱于已有终态）

    await submit_order(factory, wedap_call=_wedap_call, req=_req())
    async with factory() as s:
        order = (await s.execute(select(BankTxnOrder))).scalar_one()
        # CAS skip：未被本次 PROCESSING/SUBMITTED 覆盖，仍是回调置的 SUCCEEDED
        assert order.status == OrderStatus.SUCCEEDED
        assert order.finalized_via == "CALLBACK"


# ── order_finalize 幂等跳过 ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_finalize_idempotent_skips_when_already_finalized(factory) -> None:
    """order.finalized_at 已非空 → finalize 直接 return（不重复 audit/转发），防同步+回调双收口。"""
    async with factory() as s:
        async with s.begin():
            s.add(
                BankTxnOrder(
                    tenant_id="OCBC",
                    biz_seq_no="DSB-X",
                    business_action="DISBURSE",
                    biz_type="DSB",
                    amount=Decimal("1.0000"),
                    currency="USD",
                    caller_service="lifecycle",
                    status="SUCCEEDED",
                    finalized_at=dt.datetime.now(dt.UTC),
                    finalized_via="SYNC",
                )
            )
    async with factory() as s:
        async with s.begin():
            order = (await s.execute(select(BankTxnOrder))).scalar_one()
            await finalize_terminal_in_session(s, order=order, source="CALLBACK", trace_id="t")
    async with factory() as s:
        # 幂等：未产生第二条 outbox，finalized_via 仍是首次的 SYNC
        assert (await s.execute(select(CallbackOutbox))).all() == []
        assert (await s.execute(select(BankTxnOrder))).scalar_one().finalized_via == "SYNC"


# ── order_reconcile worker ────────────────────────────────────────────────────


async def _seed_order(factory, *, biz, status, with_leg=False) -> None:
    async with factory() as s:
        async with s.begin():
            s.add(
                BankTxnOrder(
                    tenant_id="OCBC",
                    biz_seq_no=biz,
                    business_action="DISBURSE",
                    biz_type="DSB",
                    amount=Decimal("1.0000"),
                    currency="USD",
                    caller_service="lifecycle",
                    status=status,
                    submitted_at=dt.datetime.now(dt.UTC),
                )
            )
            await s.flush()
            if with_leg:
                order = (
                    await s.execute(select(BankTxnOrder).where(BankTxnOrder.biz_seq_no == biz))
                ).scalar_one()
                s.add(
                    BankTxnLeg(
                        tenant_id="OCBC",
                        order_id=order.id,
                        biz_seq_no=biz,
                        external_ref=f"REF-{biz}",
                        step_type="DISBURSEMENT",
                        step_seq=1,
                        amount=Decimal("1.0000"),
                        currency="USD",
                        status="SUCCESS",
                    )
                )


@pytest.mark.asyncio
async def test_reconcile_once_picks_stale_nonterminal_and_terminal_without_leg(factory) -> None:
    """兜底双扫描：① 非终态 stale；② 终态但无 leg。终态有 leg 的不选。"""
    await _seed_order(factory, biz="DSB-STALE", status="SUBMITTED")  # ① 非终态
    await _seed_order(factory, biz="DSB-TERM-NOLEG", status="SUCCEEDED")  # ② 终态无 leg
    await _seed_order(factory, biz="DSB-TERM-LEG", status="SUCCEEDED", with_leg=True)  # 不选
    now = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=60)  # 让 stale_after 已过
    with patch("app.workers.order_reconcile_worker.sync_legs_for", new_callable=AsyncMock) as m:
        count = await reconcile_once(
            factory,
            wedap=AsyncMock(),
            now=now,
            stale_after_seconds=1.0,
            max_age_seconds=1e9,
            batch_limit=10,
        )
    assert count == 2
    picked = {c.kwargs["biz_seq_no"] for c in m.call_args_list}
    assert picked == {"DSB-STALE", "DSB-TERM-NOLEG"}


@pytest.mark.asyncio
async def test_reconcile_once_isolates_per_order_failure(factory) -> None:
    """单单 LegsSyncIncomplete 隔离：不中断批，不计入成功数。"""
    await _seed_order(factory, biz="DSB-FAIL", status="SUBMITTED")
    now = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=60)
    with patch(
        "app.workers.order_reconcile_worker.sync_legs_for",
        new_callable=AsyncMock,
        side_effect=LegsSyncIncomplete("boom"),
    ):
        count = await reconcile_once(
            factory,
            wedap=AsyncMock(),
            now=now,
            stale_after_seconds=1.0,
            max_age_seconds=1e9,
            batch_limit=10,
        )
    assert count == 0
