"""order-reconcile worker：回调丢失/同步终态明细缺失的兜底收敛（V2 同步优先）。

区别于 recon_worker（对账文件摄取），本 worker 专扫 bank_txn_order 两类：
  ① **非终态 stale**：status ∈ {ACCEPTED, SUBMITTED, PROCESSING, RESULT_UNKNOWN}
     且 created_at 超 stale_after —— 回调丢失导致父单永卡非终态（codex HIGH-2）。
  ② **终态但 legs 缺失**：status ∈ {SUCCEEDED, FAILED} 且无任何 leg 行 —— 同步置终态但
     明细（composite steps）随回调才到、回调又丢，ledger 缺流水（codex HIGH-3）。

逐单复用 legs.sync_legs_for（拉 steps → 单事务聚合 + 终态收口 finalize 转发，source=RECONCILE）。
单单 LegsSyncIncomplete 隔离不中断批；max_age 下界避免无限扫古单。

run_forever 为薄壳循环（pragma: no cover）；可测核心在 reconcile_once / _select_candidates。
biz_type-aware 拉取限制：sync_legs_for 经 get_composite_steps 拉明细，CLT 若 wedap 无 composite
端点则明细兜不到（仅父单终态可由同步 txnStatus 收口）——属已知运维风险（见设计文档 §6）。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.domain.states import OrderStatus
from app.models.txn import BankTxnLeg, BankTxnOrder
from app.services.legs import LegsSyncIncomplete, sync_legs_for

logger = logging.getLogger(__name__)

_NON_TERMINAL = (
    OrderStatus.ACCEPTED,
    OrderStatus.SUBMITTED,
    OrderStatus.PROCESSING,
    OrderStatus.RESULT_UNKNOWN,
)
_TERMINAL = (OrderStatus.SUCCEEDED, OrderStatus.FAILED)


async def _select_candidates(
    session: Any,
    *,
    now: dt.datetime,
    stale_after_seconds: float,
    max_age_seconds: float,
    batch_limit: int,
) -> list[tuple[str, str]]:
    """兜底候选：① 非终态且超 stale_after；② 终态但无 leg。均限 max_age 窗内。"""
    stale_before = now - timedelta(seconds=stale_after_seconds)
    min_created = now - timedelta(seconds=max_age_seconds)
    has_leg = exists().where(
        and_(
            BankTxnLeg.tenant_id == BankTxnOrder.tenant_id,
            BankTxnLeg.biz_seq_no == BankTxnOrder.biz_seq_no,
        )
    )
    cond_nonterminal = and_(
        BankTxnOrder.status.in_(_NON_TERMINAL),
        BankTxnOrder.created_at < stale_before,
    )
    cond_terminal_no_leg = and_(BankTxnOrder.status.in_(_TERMINAL), ~has_leg)
    stmt = (
        select(BankTxnOrder.tenant_id, BankTxnOrder.biz_seq_no)
        .where(
            and_(
                BankTxnOrder.created_at > min_created,
                or_(cond_nonterminal, cond_terminal_no_leg),
            )
        )
        .limit(batch_limit)
    )
    return [(r[0], r[1]) for r in (await session.execute(stmt)).all()]


async def reconcile_once(
    factory: async_sessionmaker[Any],
    *,
    wedap: Any,
    now: dt.datetime,
    stale_after_seconds: float,
    max_age_seconds: float,
    batch_limit: int,
) -> int:
    """一轮兜底：选候选 → 逐单 sync_legs_for（隔离失败）。now 由调用方传入（可测）。"""
    async with factory() as session:
        candidates = await _select_candidates(
            session,
            now=now,
            stale_after_seconds=stale_after_seconds,
            max_age_seconds=max_age_seconds,
            batch_limit=batch_limit,
        )
    count = 0
    for tenant_id, biz_seq_no in candidates:
        try:
            await sync_legs_for(
                factory,
                wedap=wedap,
                tenant_id=tenant_id,
                biz_seq_no=biz_seq_no,
                source="RECONCILE",
            )
            count += 1
        except LegsSyncIncomplete:
            logger.warning("order-reconcile incomplete %s/%s（待下轮重试）", tenant_id, biz_seq_no)
    return count


async def run_forever(  # pragma: no cover
    factory: async_sessionmaker[Any],
    *,
    wedap: Any,
    interval_seconds: float,
    stale_after_seconds: float,
    max_age_seconds: float,
    batch_limit: int,
) -> None:
    """薄壳循环：每 interval 扫一轮兜底。崩溃由 supervised 退避重启。"""
    while True:
        now = dt.datetime.now(dt.UTC)
        await reconcile_once(
            factory,
            wedap=wedap,
            now=now,
            stale_after_seconds=stale_after_seconds,
            max_age_seconds=max_age_seconds,
            batch_limit=batch_limit,
        )
        await asyncio.sleep(interval_seconds)
