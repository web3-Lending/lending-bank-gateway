"""回调路径 order 级终态收口（C5：组合交易 leg/step 对 lending 透明，不下钻明细）。

wedap 交易回调 body 的 txnStatus 即父单权威终态；本服务：

- body txnStatus 映射为终态（SUCCESS/FAILED）→ 事务内 FOR UPDATE + CAS 收口
  （复用 finalize_terminal_in_session：finalized_at/via + audit + 转发 lifecycle）
- body 无终态信息 → 回落 wedap status-query（order 级，COLL 不支持）主动收敛
- 两路都未收敛 → 抛 CallbackTerminalUnresolved，调用方让 inbox 留 RECEIVED 等重放
- 已终态但回调结论不一致 → 只告警不倒退（终态防倒退），并视为已处理（PROCESSED）
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.domain.states import (
    IllegalTransition,
    OrderStatus,
    assert_transition,
    map_wedap_txn_status,
)
from app.models.txn import BankTxnOrder
from app.services.order_finalize import finalize_terminal_in_session, is_terminal
from app.services.order_status_reconcile import resolve_terminal_via_status_query

logger = logging.getLogger(__name__)


class CallbackTerminalUnresolved(Exception):
    """回调未能收敛父单终态；inbox 须留 RECEIVED 等重放/兜底 worker 收敛。"""


async def resolve_callback_terminal(
    factory: async_sessionmaker[Any],
    *,
    wedap: Any,
    tenant_id: str,
    body: dict[str, Any],
    trace_id: str = "",
) -> None:
    """按回调 body 做 order 级终态收口。

    失败语义：order 不存在（乱序回调）/ 无终态信息且 status-query 未收敛 /
    非法状态迁移 → 抛 CallbackTerminalUnresolved，交 inbox 重放机制驱动收敛。
    """
    biz_seq_no = str(body.get("bizSeqNo", ""))
    terminal = map_wedap_txn_status(str(body.get("txnStatus", "")))

    if terminal is None:
        # body 不带终态（非终态通知 / 字段缺失）→ order 级 status-query 主动收敛。
        # COLL 无状态查询接口 → 不收敛，留 RECEIVED 等带终态的回调重放 / G6 告警。
        converged = await resolve_terminal_via_status_query(
            factory,
            wedap=wedap,
            tenant_id=tenant_id,
            biz_seq_no=biz_seq_no,
            source="CALLBACK",
        )
        if not converged:
            raise CallbackTerminalUnresolved(
                f"callback without terminal txnStatus and status-query not converged "
                f"{tenant_id}/{biz_seq_no}"
            )
        return

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
            if locked is None:
                # 乱序回调（order 尚未创建）→ 留 RECEIVED 待重放
                raise CallbackTerminalUnresolved(f"unknown order {tenant_id}/{biz_seq_no}")
            if locked.finalized_at is not None or is_terminal(locked.status):
                # 幂等：已收口。回调结论与已收口终态不一致 → 只告警不倒退（终态防倒退）。
                if OrderStatus(locked.status) != terminal:
                    logger.error(
                        "callback terminal divergence %s/%s order=%s callback=%s（保持不倒退）",
                        tenant_id,
                        biz_seq_no,
                        locked.status,
                        terminal,
                    )
                return
            try:
                assert_transition(OrderStatus(locked.status), terminal)
            except IllegalTransition as exc:
                raise CallbackTerminalUnresolved(
                    f"illegal transition {tenant_id}/{biz_seq_no} {locked.status}->{terminal}"
                ) from exc
            locked.status = terminal
            await finalize_terminal_in_session(
                session, order=locked, source="CALLBACK", trace_id=trace_id
            )
