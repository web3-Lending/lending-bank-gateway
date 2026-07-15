"""回调路径 order 级终态收口（C5，不下钻 leg/steps）：resolve_callback_terminal 全路径。"""

import datetime as dt
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.core.db import build_engine, build_session_factory
from app.models.base import Base
from app.models.callback import CallbackOutbox
from app.models.txn import BankTxnOrder
from app.services.callback_finalize import (
    CallbackTerminalUnresolved,
    resolve_callback_terminal,
)

TENANT = "OCBC"
BIZ = "DSB-20260611-0001234567890"


@pytest.fixture()
async def factory():
    engine = build_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield build_session_factory(engine)
    await engine.dispose()


async def _seed(factory, *, status="SUBMITTED", finalized_at=None, biz=BIZ) -> None:
    async with factory() as s:
        async with s.begin():
            s.add(
                BankTxnOrder(
                    tenant_id=TENANT,
                    biz_seq_no=biz,
                    business_action="DISBURSE",
                    biz_type="DSB",
                    amount=Decimal("100.0000"),
                    currency="USD",
                    caller_service="lifecycle",
                    status=status,
                    finalized_at=finalized_at,
                    trans_type="DISBURSEMENT",
                    ori_req_date="20260714",
                )
            )


async def _order(factory, biz=BIZ) -> BankTxnOrder:
    async with factory() as s:
        return (
            await s.execute(select(BankTxnOrder).where(BankTxnOrder.biz_seq_no == biz))
        ).scalar_one()


async def _outbox_count(factory) -> int:
    async with factory() as s:
        return int((await s.execute(select(func.count()).select_from(CallbackOutbox))).scalar_one())


@pytest.mark.asyncio
async def test_terminal_success_finalizes_and_forwards(factory) -> None:
    """body txnStatus=SUCCESS → CAS 收口：SUCCEEDED + finalized_via=CALLBACK + outbox 1 行。"""
    await _seed(factory)
    await resolve_callback_terminal(
        factory,
        wedap=AsyncMock(),
        tenant_id=TENANT,
        body={"bizSeqNo": BIZ, "type": "disbursement", "txnStatus": "SUCCESS"},
    )
    order = await _order(factory)
    assert order.status == "SUCCEEDED"
    assert order.finalized_via == "CALLBACK"
    assert order.finalized_at is not None
    assert await _outbox_count(factory) == 1


@pytest.mark.asyncio
async def test_terminal_failed_finalizes(factory) -> None:
    """body txnStatus=FAILED → FAILED 收口。"""
    await _seed(factory)
    await resolve_callback_terminal(
        factory,
        wedap=AsyncMock(),
        tenant_id=TENANT,
        body={"bizSeqNo": BIZ, "type": "disbursement", "txnStatus": "FAILED"},
    )
    order = await _order(factory)
    assert order.status == "FAILED"
    assert order.finalized_via == "CALLBACK"


@pytest.mark.asyncio
async def test_unknown_order_raises_unresolved(factory) -> None:
    """乱序回调（order 未建）→ CallbackTerminalUnresolved，inbox 留 RECEIVED 重放。"""
    with pytest.raises(CallbackTerminalUnresolved):
        await resolve_callback_terminal(
            factory,
            wedap=AsyncMock(),
            tenant_id=TENANT,
            body={"bizSeqNo": "NO-SUCH-BIZ", "type": "x", "txnStatus": "SUCCESS"},
        )


@pytest.mark.asyncio
async def test_already_finalized_same_status_idempotent(factory) -> None:
    """已收口同终态重放 → 幂等 no-op，不重复转发。"""
    await _seed(factory, status="SUCCEEDED", finalized_at=dt.datetime.now(dt.UTC))
    await resolve_callback_terminal(
        factory,
        wedap=AsyncMock(),
        tenant_id=TENANT,
        body={"bizSeqNo": BIZ, "type": "disbursement", "txnStatus": "SUCCESS"},
    )
    assert await _outbox_count(factory) == 0


@pytest.mark.asyncio
async def test_already_terminal_divergent_status_logs_no_regress(factory, caplog) -> None:
    """已终态但回调结论不一致 → 只告警不倒退（终态防倒退），视为已处理。"""
    await _seed(factory, status="SUCCEEDED", finalized_at=dt.datetime.now(dt.UTC))
    with caplog.at_level("ERROR"):
        await resolve_callback_terminal(
            factory,
            wedap=AsyncMock(),
            tenant_id=TENANT,
            body={"bizSeqNo": BIZ, "type": "disbursement", "txnStatus": "FAILED"},
        )
    assert "callback terminal divergence" in caplog.text
    order = await _order(factory)
    assert order.status == "SUCCEEDED"  # 不倒退


@pytest.mark.asyncio
async def test_no_txn_status_falls_back_to_status_query(factory) -> None:
    """body 无终态信息 → 回落 wedap status-query 收敛（finalized_via=CALLBACK）。"""
    await _seed(factory)
    wedap = AsyncMock()
    wedap.query_transaction_status.return_value = {"txnStatus": "SUCCESS"}
    await resolve_callback_terminal(
        factory,
        wedap=wedap,
        tenant_id=TENANT,
        body={"bizSeqNo": BIZ, "type": "disbursement"},
    )
    order = await _order(factory)
    assert order.status == "SUCCEEDED"
    assert order.finalized_via == "CALLBACK"


@pytest.mark.asyncio
async def test_no_txn_status_order_already_finalized_is_processed(factory) -> None:
    """codex P1 回归：无 txnStatus 回调 + order 已被同步路径收口 → 视为已处理（不抛、不重复转发）。

    修前：status-query 对已终态返 False → 误判未收敛 → inbox 永留 RECEIVED。
    """
    await _seed(factory, status="SUCCEEDED", finalized_at=dt.datetime.now(dt.UTC))
    wedap = AsyncMock()
    await resolve_callback_terminal(
        factory,
        wedap=wedap,
        tenant_id=TENANT,
        body={"bizSeqNo": BIZ, "type": "collection"},
    )
    wedap.query_transaction_status.assert_not_called()  # 已收口，无需外呼
    assert await _outbox_count(factory) == 0  # 不重复转发


@pytest.mark.asyncio
async def test_coll_no_txn_status_nonterminal_raises_for_replay(factory) -> None:
    """COLL 无 txnStatus + status-query UNSUPPORTED + order 非终态 → 留 RECEIVED 等重放；
    order 侧超窗由 G6 stuck alert 兜底告警（非静默）。"""
    from app.clients.wedap import WedapError

    await _seed(factory, status="SUBMITTED")
    wedap = AsyncMock()
    wedap.query_transaction_status.side_effect = WedapError("UNSUPPORTED", "no status api")
    with pytest.raises(CallbackTerminalUnresolved):
        await resolve_callback_terminal(
            factory,
            wedap=wedap,
            tenant_id=TENANT,
            body={"bizSeqNo": BIZ, "type": "collection"},
        )
    order = await _order(factory)
    assert order.status == "SUBMITTED"  # 未被误推进


@pytest.mark.asyncio
async def test_no_txn_status_toctou_concurrent_finalize_is_processed(factory) -> None:
    """codex P2 回归（TOCTOU）：首读非终态后被并发路径收口、status-query 返 False →
    复读发现已收口 → 视为已处理，不抛不滞留。"""
    await _seed(factory)
    wedap = AsyncMock()

    async def _query_and_concurrently_finalize(**kwargs):
        # 模拟并发：status-query 期间同步路径完成收口
        async with factory() as s:
            async with s.begin():
                order = (
                    await s.execute(select(BankTxnOrder).where(BankTxnOrder.biz_seq_no == BIZ))
                ).scalar_one()
                order.status = "SUCCEEDED"
                order.finalized_at = dt.datetime.now(dt.UTC)
                order.finalized_via = "SYNC"
        return {"txnStatus": "PROCESSING"}  # helper 内 CAS 会发现已收口 → 返 False

    wedap.query_transaction_status.side_effect = _query_and_concurrently_finalize
    await resolve_callback_terminal(
        factory,
        wedap=wedap,
        tenant_id=TENANT,
        body={"bizSeqNo": BIZ, "type": "collection"},
    )  # 不抛
    order = await _order(factory)
    assert order.finalized_via == "SYNC"  # 收口归并发路径，本回调只是幂等确认
    assert await _outbox_count(factory) == 0


@pytest.mark.asyncio
async def test_no_txn_status_not_converged_raises(factory) -> None:
    """body 无终态 + status-query 也非终态 → CallbackTerminalUnresolved 待重放。"""
    await _seed(factory)
    wedap = AsyncMock()
    wedap.query_transaction_status.return_value = {"txnStatus": "PROCESSING"}
    with pytest.raises(CallbackTerminalUnresolved):
        await resolve_callback_terminal(
            factory,
            wedap=wedap,
            tenant_id=TENANT,
            body={"bizSeqNo": BIZ, "type": "disbursement"},
        )
    order = await _order(factory)
    assert order.status == "SUBMITTED"


@pytest.mark.asyncio
async def test_illegal_transition_raises_unresolved(factory) -> None:
    """非法迁移（PARTIALLY_REVERSED 仅允许 → REVERSED，FAILED 回调非法）→ Unresolved。"""
    await _seed(factory, status="PARTIALLY_REVERSED")
    with pytest.raises(CallbackTerminalUnresolved):
        await resolve_callback_terminal(
            factory,
            wedap=AsyncMock(),
            tenant_id=TENANT,
            body={"bizSeqNo": BIZ, "type": "disbursement", "txnStatus": "FAILED"},
        )


@pytest.mark.asyncio
async def test_reversed_order_success_callback_is_idempotent_divergence(factory) -> None:
    """REVERSED 已是终态（§3.6 counter 冲正）：SUCCESS 回调 → 幂等返回（分歧只告警不倒退）。"""
    await _seed(factory, status="REVERSED")
    await resolve_callback_terminal(
        factory,
        wedap=AsyncMock(),
        tenant_id=TENANT,
        body={"bizSeqNo": BIZ, "type": "disbursement", "txnStatus": "SUCCESS"},
    )
    order = await _order(factory)
    assert order.status == "REVERSED"  # 不被 SUCCESS 回调倒退
