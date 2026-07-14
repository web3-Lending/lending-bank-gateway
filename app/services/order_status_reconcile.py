"""G2：RESULT_UNKNOWN / 非终态父单经 wedap status-query 主动复查收敛（order 级，C5）。

本服务调 wedap.query_funds_status 直接查父单终态：

- 事务外查 wedap（DISB/RPMT/DIST 支持；COLL 等 UNSUPPORTED → 返 False 交 G6 升级）
- 事务内 FOR UPDATE 重读父单做 CAS：已终态 / finalized_at 非空 → 跳过（防非法迁移与双转发）
- 仅当前态非终态且映射为终态时 assert_transition + finalize（复用同步/回调同一收口）
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.clients.wedap import WedapError
from app.domain.states import (
    IllegalTransition,
    OrderStatus,
    assert_transition,
    map_wedap_txn_status,
)
from app.models.txn import BankTxnOrder
from app.services.order_finalize import finalize_terminal_in_session, is_terminal

logger = logging.getLogger(__name__)


async def resolve_terminal_via_status_query(
    factory: async_sessionmaker[Any],
    *,
    wedap: Any,
    tenant_id: str,
    biz_seq_no: str,
    source: str = "RECONCILE",
) -> bool:
    """对非终态父单经 wedap status-query 收敛终态。

    返回 True=本次收敛到终态；False=未收敛（不支持 / 暂态 / 已终态 / 非终态结果）。
    """
    # 1. 事务外轻量读 biz_type
    async with factory() as session:
        order = (
            await session.execute(
                select(BankTxnOrder).where(
                    BankTxnOrder.tenant_id == tenant_id,
                    BankTxnOrder.biz_seq_no == biz_seq_no,
                )
            )
        ).scalar_one_or_none()
        if order is None:
            return False
        biz_type = order.biz_type

    # 2. 事务外查 wedap；UNSUPPORTED（COLL 归集）/ 暂态 HTTP 错误 → 不收敛，交下轮 / G6
    try:
        data = await wedap.query_funds_status(
            tenant_id=tenant_id,
            request_id=f"status-reconcile-{biz_seq_no}",
            biz_seq_no=biz_seq_no,
            biz_type=biz_type,
        )
    except (WedapError, httpx.HTTPError):
        return False

    terminal = map_wedap_txn_status(str(data.get("txnStatus", "")))
    if terminal is None:
        return False  # 非终态结果 → no-op，等下轮

    # 3. 事务内 FOR UPDATE 重读 + CAS：已终态 / 已收口 → 跳过，防非法迁移与双转发
    async with factory() as session:
        async with session.begin():
            locked = (
                await session.execute(
                    select(BankTxnOrder)
                    .where(
                        BankTxnOrder.tenant_id == tenant_id,
                        BankTxnOrder.biz_seq_no == biz_seq_no,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if locked is None:  # pragma: no cover - 两读之间被删，竞态防御
                return False
            if locked.finalized_at is not None or is_terminal(locked.status):
                return False
            try:
                assert_transition(OrderStatus(locked.status), terminal)
            except IllegalTransition:  # pragma: no cover - 非终态候选→终态均合法，防御分支
                logger.warning(
                    "status-reconcile illegal transition %s/%s %s->%s",
                    tenant_id,
                    biz_seq_no,
                    locked.status,
                    terminal,
                )
                return False
            locked.status = terminal
            await finalize_terminal_in_session(
                session,
                order=locked,
                source=source,
                trace_id=f"reconcile-{biz_seq_no}",
            )
    return True
