"""同步优先终态收口 V2 新行为测试：submit CAS/映射、order_finalize 幂等、order_reconcile worker。"""

import datetime as dt
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.clients.wedap import WedapError
from app.core.db import build_engine, build_session_factory
from app.domain.states import OrderStatus, map_wedap_txn_status
from app.models.audit import AuditLog
from app.models.base import Base
from app.models.callback import CallbackOutbox
from app.models.order_alert import OrderStuckAlert
from app.models.txn import BankTxnLeg, BankTxnOrder
from app.services.legs import LegsSyncIncomplete
from app.services.order_finalize import finalize_terminal_in_session
from app.services.order_status_reconcile import resolve_terminal_via_status_query
from app.services.submit import SubmitRequest, submit_order
from app.workers.order_reconcile_worker import alert_stuck_orders, reconcile_once


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


async def _seed_order(factory, *, biz, status, with_leg=False, finalized_at=None) -> None:
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
                    finalized_at=finalized_at,
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
    await _seed_order(
        factory, biz="DSB-TERM-NOLEG", status="SUCCEEDED", finalized_at=dt.datetime.now(dt.UTC)
    )  # ② 终态无 leg（finalized 在 backfill 窗内）
    await _seed_order(factory, biz="DSB-TERM-LEG", status="SUCCEEDED", with_leg=True)  # 不选
    now = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=60)  # 让 stale_after 已过
    wedap = AsyncMock()
    wedap.query_funds_status.return_value = {"txnStatus": "PROCESSING"}  # 非终态→回落 leg 兜底
    with patch("app.workers.order_reconcile_worker.sync_legs_for", new_callable=AsyncMock) as m:
        count = await reconcile_once(
            factory,
            wedap=wedap,
            now=now,
            stale_after_seconds=1.0,
            max_age_seconds=1e9,
            leg_backfill_seconds=1e9,
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
    wedap = AsyncMock()
    wedap.query_funds_status.return_value = {"txnStatus": "PROCESSING"}  # 非终态→回落 leg 兜底
    with patch(
        "app.workers.order_reconcile_worker.sync_legs_for",
        new_callable=AsyncMock,
        side_effect=LegsSyncIncomplete("boom"),
    ):
        count = await reconcile_once(
            factory,
            wedap=wedap,
            now=now,
            stale_after_seconds=1.0,
            max_age_seconds=1e9,
            leg_backfill_seconds=1e9,
            batch_limit=10,
        )
    assert count == 0


@pytest.mark.asyncio
async def test_reconcile_skips_terminal_no_leg_beyond_backfill_window(factory) -> None:
    """终态无 leg 但 finalized_at 超 leg_backfill 窗 → 不再补拉（防 CLT 空 steps 长期热重试）。"""
    now = dt.datetime.now(dt.UTC)
    await _seed_order(
        factory,
        biz="DSB-OLD-TERM",
        status="SUCCEEDED",
        finalized_at=now - dt.timedelta(hours=2),  # 2h 前收口
    )
    with patch("app.workers.order_reconcile_worker.sync_legs_for", new_callable=AsyncMock) as m:
        count = await reconcile_once(
            factory,
            wedap=AsyncMock(),
            now=now,
            stale_after_seconds=1.0,
            max_age_seconds=1e9,
            leg_backfill_seconds=3600.0,  # 1h 窗，2h 前的终态单不选
            batch_limit=10,
        )
    assert count == 0
    assert m.call_count == 0


@pytest.mark.asyncio
async def test_reconcile_once_isolates_missing_txn_date(factory) -> None:
    """G1 worker 级回归：候选单 steps 缺 txnDate → 真实 apply_legs 抛 LegsSyncIncomplete →
    reconcile_once 单笔隔离(count=0)，不逸出 KeyError/InvalidOperation 打穿整轮 worker。"""
    await _seed_order(factory, biz="DSB-NOTXN", status="SUBMITTED")  # 非终态 stale
    step_no_date = {
        "stepSeq": 1,
        "sysRefNo": "R1",
        "stepType": "DISBURSEMENT_COLLECTION",
        "amount": "60.0000",
        "currencyCode": "USD",
        "status": "SUCCESS",
        # 故意无 txnDate
    }
    wedap = AsyncMock()
    wedap.get_composite_steps.return_value = [step_no_date]
    wedap.query_funds_status.return_value = {"txnStatus": "PROCESSING"}  # 非终态→回落 leg 兜底
    now = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=60)
    count = await reconcile_once(
        factory,
        wedap=wedap,
        now=now,
        stale_after_seconds=1.0,
        max_age_seconds=1e9,
        leg_backfill_seconds=1e9,
        batch_limit=10,
    )
    assert count == 0  # 隔离，未计成功
    async with factory() as s:
        legs = (await s.execute(select(BankTxnLeg))).scalars().all()
    assert legs == []  # 整批回滚，无部分 leg


# ---------------------------------------------------------------------------
# G2：map_wedap_txn_status 共享映射 + status-query 主动收敛
# ---------------------------------------------------------------------------


def test_map_wedap_txn_status() -> None:
    """SUCCESS→SUCCEEDED、FAILED→FAILED、其余/未知/空→None（调用方决定回落）。"""
    assert map_wedap_txn_status("SUCCESS") == OrderStatus.SUCCEEDED
    assert map_wedap_txn_status("success") == OrderStatus.SUCCEEDED
    assert map_wedap_txn_status("FAILED") == OrderStatus.FAILED
    assert map_wedap_txn_status("PROCESSING") is None
    assert map_wedap_txn_status("") is None
    assert map_wedap_txn_status("WHATEVER") is None


@pytest.mark.asyncio
async def test_resolve_status_query_success_finalizes(factory) -> None:
    """G2：RESULT_UNKNOWN 父单 status-query 返 SUCCESS → 锁内收敛 SUCCEEDED + 转发一次。"""
    await _seed_order(factory, biz="DSB-RU", status="RESULT_UNKNOWN")
    wedap = AsyncMock()
    wedap.query_funds_status.return_value = {"txnStatus": "SUCCESS"}
    ok = await resolve_terminal_via_status_query(
        factory, wedap=wedap, tenant_id="OCBC", biz_seq_no="DSB-RU"
    )
    assert ok is True
    async with factory() as s:
        order = (
            await s.execute(select(BankTxnOrder).where(BankTxnOrder.biz_seq_no == "DSB-RU"))
        ).scalar_one()
        outbox = (await s.execute(select(CallbackOutbox))).scalars().all()
    assert order.status == OrderStatus.SUCCEEDED
    assert order.finalized_at is not None and order.finalized_via == "RECONCILE"
    assert len(outbox) == 1


@pytest.mark.asyncio
async def test_resolve_status_query_skips_already_finalized(factory) -> None:
    """G2 CAS：父单已终态/已收口 → 锁内重读跳过，不重复转发（防 SUCCEEDED→SUCCEEDED）。"""
    await _seed_order(
        factory, biz="DSB-DONE", status="SUCCEEDED", finalized_at=dt.datetime.now(dt.UTC)
    )
    wedap = AsyncMock()
    wedap.query_funds_status.return_value = {"txnStatus": "SUCCESS"}
    ok = await resolve_terminal_via_status_query(
        factory, wedap=wedap, tenant_id="OCBC", biz_seq_no="DSB-DONE"
    )
    assert ok is False
    async with factory() as s:
        outbox = (await s.execute(select(CallbackOutbox))).scalars().all()
    assert outbox == []


@pytest.mark.asyncio
async def test_resolve_status_query_unsupported_returns_false(factory) -> None:
    """G2：CLT 等 query_funds_status UNSUPPORTED → 返 False（交 G6），父单不变。"""
    await _seed_order(factory, biz="CLT-X", status="RESULT_UNKNOWN")
    wedap = AsyncMock()
    wedap.query_funds_status.side_effect = WedapError("UNSUPPORTED", "no status api")
    ok = await resolve_terminal_via_status_query(
        factory, wedap=wedap, tenant_id="OCBC", biz_seq_no="CLT-X"
    )
    assert ok is False
    async with factory() as s:
        order = (
            await s.execute(select(BankTxnOrder).where(BankTxnOrder.biz_seq_no == "CLT-X"))
        ).scalar_one()
    assert order.status == OrderStatus.RESULT_UNKNOWN


@pytest.mark.asyncio
async def test_resolve_status_query_nonterminal_noop(factory) -> None:
    """G2：status-query 返非终态(PROCESSING) → no-op，父单不变。"""
    await _seed_order(factory, biz="DSB-PROC", status="RESULT_UNKNOWN")
    wedap = AsyncMock()
    wedap.query_funds_status.return_value = {"txnStatus": "PROCESSING"}
    ok = await resolve_terminal_via_status_query(
        factory, wedap=wedap, tenant_id="OCBC", biz_seq_no="DSB-PROC"
    )
    assert ok is False
    async with factory() as s:
        order = (
            await s.execute(select(BankTxnOrder).where(BankTxnOrder.biz_seq_no == "DSB-PROC"))
        ).scalar_one()
    assert order.status == OrderStatus.RESULT_UNKNOWN


@pytest.mark.asyncio
async def test_resolve_status_query_order_not_found(factory) -> None:
    """G2：父单不存在 → 返 False，不外呼 wedap。"""
    wedap = AsyncMock()
    ok = await resolve_terminal_via_status_query(
        factory, wedap=wedap, tenant_id="OCBC", biz_seq_no="NOPE"
    )
    assert ok is False
    wedap.query_funds_status.assert_not_called()


@pytest.mark.asyncio
async def test_reconcile_once_converges_via_status_query(factory) -> None:
    """G2：非终态 stale 单经 status-query 收敛终态，免去 leg 兜底（sync_legs_for 不被调用）。"""
    await _seed_order(factory, biz="DSB-SQ", status="RESULT_UNKNOWN")
    wedap = AsyncMock()
    wedap.query_funds_status.return_value = {"txnStatus": "SUCCESS"}
    now = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=60)
    with patch("app.workers.order_reconcile_worker.sync_legs_for", new_callable=AsyncMock) as m:
        count = await reconcile_once(
            factory,
            wedap=wedap,
            now=now,
            stale_after_seconds=1.0,
            max_age_seconds=1e9,
            leg_backfill_seconds=1e9,
            batch_limit=10,
        )
    assert count == 1
    m.assert_not_called()
    async with factory() as s:
        order = (
            await s.execute(select(BankTxnOrder).where(BankTxnOrder.biz_seq_no == "DSB-SQ"))
        ).scalar_one()
    assert order.status == OrderStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_reconcile_once_isolates_status_query_unexpected_error(factory) -> None:
    """G2 隔离：status-query 抛非预期异常 → 单笔隔离回落 leg 兜底，不打穿整轮。"""
    await _seed_order(factory, biz="DSB-SQ-ERR", status="RESULT_UNKNOWN")
    wedap = AsyncMock()
    wedap.query_funds_status.side_effect = RuntimeError("boom")  # resolve 不吞 → worker 兜
    now = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=60)
    with patch("app.workers.order_reconcile_worker.sync_legs_for", new_callable=AsyncMock) as m:
        count = await reconcile_once(
            factory,
            wedap=wedap,
            now=now,
            stale_after_seconds=1.0,
            max_age_seconds=1e9,
            leg_backfill_seconds=1e9,
            batch_limit=10,
        )
    assert count == 1  # 回落 leg 兜底成功
    m.assert_called_once()  # 走了 leg 路径


# ---------------------------------------------------------------------------
# G6：stuck-order 去重告警
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_alert_stuck_orders_dedups(factory) -> None:
    """G6：超 max_age 非终态父单 → 首轮新增告警(+ERROR)；次轮 UNIQUE 去重不重复。"""
    await _seed_order(factory, biz="DSB-STUCK", status="RESULT_UNKNOWN")
    now = dt.datetime.now(dt.UTC) + dt.timedelta(days=10)  # 远超 max_age
    n1 = await alert_stuck_orders(factory, now=now, max_age_seconds=604800.0, batch_limit=10)
    assert n1 == 1
    n2 = await alert_stuck_orders(factory, now=now, max_age_seconds=604800.0, batch_limit=10)
    assert n2 == 0  # UNIQUE(tenant,biz) 去重
    async with factory() as s:
        alerts = (await s.execute(select(OrderStuckAlert))).scalars().all()
    assert len(alerts) == 1
    assert alerts[0].biz_seq_no == "DSB-STUCK" and alerts[0].biz_type == "DSB"


@pytest.mark.asyncio
async def test_alert_stuck_orders_skips_within_window_and_terminal(factory) -> None:
    """G6：窗内非终态 / 终态单 不告警。"""
    await _seed_order(factory, biz="DSB-FRESH", status="SUBMITTED")  # 窗内
    await _seed_order(factory, biz="DSB-DONE2", status="SUCCEEDED")  # 终态
    now = dt.datetime.now(dt.UTC)  # max_age 内
    n = await alert_stuck_orders(factory, now=now, max_age_seconds=604800.0, batch_limit=10)
    assert n == 0
    async with factory() as s:
        assert (await s.execute(select(OrderStuckAlert))).scalars().all() == []
