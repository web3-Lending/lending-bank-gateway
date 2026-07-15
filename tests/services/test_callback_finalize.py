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


def _identity_resp(kwargs, status):
    """mock 通用状态响应：回显请求身份，配 reconcile 响应身份核对。"""
    return {
        "txnStatus": status,
        "oriBizSeqNo": kwargs["ori_biz_seq_no"],
        "transType": kwargs["trans_type"],
    }


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
    # 9000 bank_inbound schema 硬校验 bizSeqNo + type（dev-hw 2026-07-15 replay 实测缺
    # type 被 400 INVALID_REQUEST 拒），type 取 order.business_action
    async with factory() as s:
        row = (await s.execute(select(CallbackOutbox))).scalar_one()
    assert row.payload == {
        "bizSeqNo": BIZ,
        "type": "DISBURSE",
        "txnStatus": "SUCCEEDED",
        "finalizedVia": "CALLBACK",
    }


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
    wedap.query_transaction_status.side_effect = lambda **kw: _identity_resp(kw, "SUCCESS")
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
    语义更新（reversal ingestion）：已收口 SUCCEEDED 单会经 status-query 做一次
    reversal 核查（生产升级入口）——wedap 仍 SUCCESS 时零转发、状态不变、不抛。
    """
    await _seed(factory, status="SUCCEEDED", finalized_at=dt.datetime.now(dt.UTC))
    wedap = AsyncMock()
    wedap.query_transaction_status.side_effect = lambda **kw: _identity_resp(kw, "SUCCESS")
    await resolve_callback_terminal(
        factory,
        wedap=wedap,
        tenant_id=TENANT,
        body={"bizSeqNo": BIZ, "type": "collection"},
    )
    wedap.query_transaction_status.assert_called_once()  # reversal 核查恰好一次
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
        # 回显正确身份（过身份核对），让 CAS 分支成为唯一止损点
        return {
            "txnStatus": "PROCESSING",
            "oriBizSeqNo": kwargs["ori_biz_seq_no"],
            "transType": kwargs["trans_type"],
        }

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
    # 回显正确身份（过身份核对），断言确因 PROCESSING 非终态而不收敛
    wedap.query_transaction_status.side_effect = lambda **kw: _identity_resp(kw, "PROCESSING")
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
async def test_nonterminal_reversed_callback_finalizes(factory) -> None:
    """非终态单收到 REVERSED 回调（counter 冲正）→ 收敛 REVERSED + 转发一次（§3.6）。"""
    await _seed(factory, status="PROCESSING")
    await resolve_callback_terminal(
        factory,
        wedap=AsyncMock(),
        tenant_id=TENANT,
        body={"bizSeqNo": BIZ, "type": "disbursement", "txnStatus": "REVERSED"},
    )
    order = await _order(factory)
    assert order.status == "REVERSED"
    assert order.finalized_via == "CALLBACK"
    assert await _outbox_count(factory) == 1


@pytest.mark.asyncio
async def test_partially_reversed_to_reversed_callback_finalizes(factory) -> None:
    """PARTIALLY_REVERSED（未收口）→ REVERSED 回调合法收敛。"""
    await _seed(factory, status="PARTIALLY_REVERSED")
    await resolve_callback_terminal(
        factory,
        wedap=AsyncMock(),
        tenant_id=TENANT,
        body={"bizSeqNo": BIZ, "type": "disbursement", "txnStatus": "REVERSED"},
    )
    order = await _order(factory)
    assert order.status == "REVERSED"


@pytest.mark.asyncio
async def test_succeeded_order_reversed_callback_upgrades_and_forwards_once(factory) -> None:
    """reversal ingestion：SUCCEEDED 已收口单收到 REVERSED 回调（counter 冲正）→ 升级
    REVERSED + audit(upgraded_from) + 按状态键二次转发一次；重复 REVERSED 回调幂等零增量。"""
    await _seed(factory, status="SUCCEEDED", finalized_at=dt.datetime.now(dt.UTC))
    body = {"bizSeqNo": BIZ, "type": "disbursement", "txnStatus": "REVERSED"}
    await resolve_callback_terminal(factory, wedap=AsyncMock(), tenant_id=TENANT, body=body)
    order = await _order(factory)
    assert order.status == "REVERSED"
    assert order.finalized_via == "CALLBACK"
    assert order.finalized_at is not None  # 首次收口时间保留（非空即可）
    assert await _outbox_count(factory) == 1  # fwd-…-REVERSED 新键，转发一次
    # 重复冲正回调：status 已是 REVERSED → 幂等 return，零新转发
    await resolve_callback_terminal(factory, wedap=AsyncMock(), tenant_id=TENANT, body=body)
    assert await _outbox_count(factory) == 1


@pytest.mark.asyncio
async def test_no_terminal_callback_on_finalized_succeeded_triggers_reversal_check(
    factory,
) -> None:
    """P1 生产链可达性：无终态回调撞已收口 SUCCEEDED 单 → 经 status-query 复查，
    wedap 回 REVERSED 则升级（这是 reconcile 升级分支在生产的真实入口）。"""
    await _seed(factory, status="SUCCEEDED", finalized_at=dt.datetime.now(dt.UTC))
    wedap = AsyncMock()
    wedap.query_transaction_status.side_effect = lambda **kw: _identity_resp(kw, "REVERSED")
    await resolve_callback_terminal(
        factory,
        wedap=wedap,
        tenant_id=TENANT,
        body={"bizSeqNo": BIZ, "type": "disbursement"},  # 无 txnStatus
    )  # 不抛
    order = await _order(factory)
    assert order.status == "REVERSED"
    assert await _outbox_count(factory) == 1


@pytest.mark.asyncio
async def test_no_terminal_callback_on_finalized_succeeded_still_success_noop(
    factory,
) -> None:
    """无终态回调撞已收口 SUCCEEDED 单，wedap 仍 SUCCESS → no-op 幂等返回（不抛不转发）。"""
    await _seed(factory, status="SUCCEEDED", finalized_at=dt.datetime.now(dt.UTC))
    wedap = AsyncMock()
    wedap.query_transaction_status.side_effect = lambda **kw: _identity_resp(kw, "SUCCESS")
    await resolve_callback_terminal(
        factory,
        wedap=wedap,
        tenant_id=TENANT,
        body={"bizSeqNo": BIZ, "type": "disbursement"},
    )
    order = await _order(factory)
    assert order.status == "SUCCEEDED"
    assert await _outbox_count(factory) == 0


@pytest.mark.asyncio
async def test_no_terminal_callback_on_finalized_failed_skips_query(factory) -> None:
    """无终态回调撞已收口 FAILED 单 → 零外呼直接幂等返回（仅 SUCCEEDED 做 reversal 核查）。"""
    await _seed(factory, status="FAILED", finalized_at=dt.datetime.now(dt.UTC))
    wedap = AsyncMock()
    await resolve_callback_terminal(
        factory,
        wedap=wedap,
        tenant_id=TENANT,
        body={"bizSeqNo": BIZ, "type": "collection"},
    )
    wedap.query_transaction_status.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("absorbing", ["CANCELLED", "EXPIRED"])
async def test_cancelled_expired_reversed_callback_divergence_not_stuck(
    factory, absorbing: str
) -> None:
    """P2 吸收态：CANCELLED/EXPIRED（finalized_at 为空）收到 REVERSED 回调 → divergence
    告警后视为已处理（不抛 → inbox 不会永留 RECEIVED 重放），状态不变零转发。"""
    await _seed(factory, status=absorbing)  # finalized_at=None
    await resolve_callback_terminal(
        factory,
        wedap=AsyncMock(),
        tenant_id=TENANT,
        body={"bizSeqNo": BIZ, "type": "disbursement", "txnStatus": "REVERSED"},
    )  # 关键断言：不抛 CallbackTerminalUnresolved
    order = await _order(factory)
    assert order.status == absorbing
    assert await _outbox_count(factory) == 0


@pytest.mark.asyncio
async def test_failed_order_reversed_callback_stays_failed(factory) -> None:
    """FAILED 无资金动作不可冲正：REVERSED 回调只 divergence 告警，不升级不转发。"""
    await _seed(factory, status="FAILED", finalized_at=dt.datetime.now(dt.UTC))
    await resolve_callback_terminal(
        factory,
        wedap=AsyncMock(),
        tenant_id=TENANT,
        body={"bizSeqNo": BIZ, "type": "disbursement", "txnStatus": "REVERSED"},
    )
    order = await _order(factory)
    assert order.status == "FAILED"
    assert await _outbox_count(factory) == 0


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
