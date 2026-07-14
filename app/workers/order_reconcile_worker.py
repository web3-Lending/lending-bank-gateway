"""order-reconcile worker：回调丢失导致父单卡非终态的兜底收敛（order 级，C5 不下钻 leg）。

区别于 recon_worker（对账文件摄取），本 worker 专扫 bank_txn_order 的**非终态 stale** 单：
status ∈ {ACCEPTED, SUBMITTED, PROCESSING, RESULT_UNKNOWN} 且 created_at 超 stale_after
—— 回调丢失导致父单永卡非终态（codex HIGH-2）。

逐单经 wedap status-query（order 级，DISB/RPMT/DIST 支持）主动收敛终态；COLL 无状态查询
接口 → 只能靠回调收口，超 max_age 仍未终态由 G6 stuck alert 显式告警交人工处置。

run_forever 为薄壳循环（pragma: no cover）；可测核心在 reconcile_once / _select_candidates。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.domain.states import OrderStatus
from app.models.order_alert import OrderStuckAlert
from app.models.txn import BankTxnOrder
from app.services.order_status_reconcile import resolve_terminal_via_status_query

logger = logging.getLogger(__name__)

_NON_TERMINAL = (
    OrderStatus.ACCEPTED,
    OrderStatus.SUBMITTED,
    OrderStatus.PROCESSING,
    OrderStatus.RESULT_UNKNOWN,
)


async def _select_candidates(
    session: Any,
    *,
    now: dt.datetime,
    stale_after_seconds: float,
    max_age_seconds: float,
    batch_limit: int,
) -> list[tuple[str, str, str]]:
    """兜底候选：非终态 stale（created_at 在 max_age~stale_after 窗）。"""
    stale_before = now - timedelta(seconds=stale_after_seconds)
    min_created = now - timedelta(seconds=max_age_seconds)
    stmt = (
        select(BankTxnOrder.tenant_id, BankTxnOrder.biz_seq_no, BankTxnOrder.status)
        .where(
            and_(
                BankTxnOrder.status.in_(_NON_TERMINAL),
                BankTxnOrder.created_at < stale_before,
                BankTxnOrder.created_at > min_created,
            )
        )
        .limit(batch_limit)
    )
    return [(r[0], r[1], r[2]) for r in (await session.execute(stmt)).all()]


async def reconcile_once(
    factory: async_sessionmaker[Any],
    *,
    wedap: Any,
    now: dt.datetime,
    stale_after_seconds: float,
    max_age_seconds: float,
    batch_limit: int,
) -> int:
    """一轮兜底：选候选 → 逐单 status-query 收敛（隔离失败）。now 由调用方传入（可测）。"""
    async with factory() as session:
        candidates = await _select_candidates(
            session,
            now=now,
            stale_after_seconds=stale_after_seconds,
            max_age_seconds=max_age_seconds,
            batch_limit=batch_limit,
        )
    count = 0
    for tenant_id, biz_seq_no, _status in candidates:
        # G2：非终态父单经 wedap status-query（order 级）主动收敛终态。
        # 单笔隔离：意外异常（DB / 脏状态 ValueError / 其它）不打穿整轮
        # （resolve 内部已吞 WedapError/httpx，此处兜未预期异常），待下轮重试。
        try:
            if await resolve_terminal_via_status_query(
                factory, wedap=wedap, tenant_id=tenant_id, biz_seq_no=biz_seq_no
            ):
                count += 1
            else:
                logger.warning(
                    "order-reconcile not converged %s/%s（待下轮重试 / G6 告警）",
                    tenant_id,
                    biz_seq_no,
                )
        except Exception:  # noqa: BLE001 - 兜底 worker 单笔隔离，任何意外待下轮重试
            logger.exception("status-reconcile failed %s/%s（单笔隔离）", tenant_id, biz_seq_no)
    # G6：超 max_age 仍非终态的父单 → 去重告警（标记表 + 限频 ERROR），不静默放弃
    await alert_stuck_orders(
        factory, now=now, max_age_seconds=max_age_seconds, batch_limit=batch_limit
    )
    return count


async def _record_stuck_alert(
    factory: async_sessionmaker[Any],
    *,
    tenant_id: str,
    biz_seq_no: str,
    biz_type: str | None,
    now: dt.datetime,
) -> bool:
    """插入 stuck 告警标记；UNIQUE(tenant,biz) 命中（已告警过）→ 返 False。

    INSERT + 捕获 IntegrityError 跨 SQLite/MySQL 可移植地实现「只告警一次」去重，
    不依赖方言专属的 ON CONFLICT / INSERT IGNORE。
    """
    try:
        async with factory() as session:
            async with session.begin():
                session.add(
                    OrderStuckAlert(
                        tenant_id=tenant_id,
                        biz_seq_no=biz_seq_no,
                        biz_type=biz_type,
                        first_alerted_at=now,
                    )
                )
        return True
    except IntegrityError:
        return False


async def alert_stuck_orders(
    factory: async_sessionmaker[Any],
    *,
    now: dt.datetime,
    max_age_seconds: float,
    batch_limit: int,
) -> int:
    """G6：超 max_age 仍非终态的父单 → 去重告警。返回本轮新增告警数。

    这些单已超出 reconcile 候选窗（_select_candidates 不再复查，原会静默放弃）。本函数把它们
    显式记入 order_stuck_alert（唯一约束去重）并限频 ERROR（仅首次告警记一行），交人工处置。
    """
    min_created = now - timedelta(seconds=max_age_seconds)
    async with factory() as session:
        rows = (
            await session.execute(
                select(
                    BankTxnOrder.tenant_id,
                    BankTxnOrder.biz_seq_no,
                    BankTxnOrder.biz_type,
                )
                .where(
                    and_(
                        BankTxnOrder.status.in_(_NON_TERMINAL),
                        BankTxnOrder.created_at <= min_created,
                    )
                )
                .limit(batch_limit)
            )
        ).all()
    new_alerts = 0
    for tenant_id, biz_seq_no, biz_type in rows:
        if await _record_stuck_alert(
            factory,
            tenant_id=tenant_id,
            biz_seq_no=biz_seq_no,
            biz_type=biz_type,
            now=now,
        ):
            new_alerts += 1
            logger.error(
                "order stuck unresolved over max_age %s/%s biz_type=%s（已记 order_stuck_alert）",
                tenant_id,
                biz_seq_no,
                biz_type,
            )
    return new_alerts


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
