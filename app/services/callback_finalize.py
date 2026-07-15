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
    ABSORBING_STATUSES,
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
        # body 不带终态（非终态通知 / 字段缺失）。
        # 先看 order 现状：已被同步/兜底路径收口 → 本回调视为已处理（幂等成功），
        # 不能依赖 status-query 的布尔返回——它对「已终态」也返 False，会把正常
        # 双出口回调误判成未收敛、inbox 永留 RECEIVED（codex P1）。
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
            raise CallbackTerminalUnresolved(f"unknown order {tenant_id}/{biz_seq_no}")
        if order.finalized_at is not None or is_terminal(order.status):
            # 已收口 SUCCEEDED 单：无终态回调是 reversal 核查的生产入口（codex P1——
            # reconcile worker 只扫非终态，SUCCEEDED 单否则永无升级触发链）——经
            # status-query 复查一次，wedap 若回 REVERSED 则走锁内升级；其余结果 no-op。
            # 运营/上游重放一条无终态回调即可触发核查。失败/异常不影响幂等语义（吞掉）。
            if OrderStatus(order.status) == OrderStatus.SUCCEEDED:
                await resolve_terminal_via_status_query(
                    factory,
                    wedap=wedap,
                    tenant_id=tenant_id,
                    biz_seq_no=biz_seq_no,
                    source="CALLBACK",
                )
            return
        # 非终态 → order 级 status-query 主动收敛。COLL 无状态查询接口 → 不收敛，
        # 留 RECEIVED 等带终态的回调重放；order 侧超窗由 G6 stuck alert 兜底告警。
        converged = await resolve_terminal_via_status_query(
            factory,
            wedap=wedap,
            tenant_id=tenant_id,
            biz_seq_no=biz_seq_no,
            source="CALLBACK",
        )
        if converged:
            return
        # TOCTOU（codex P2）：首读非终态后，同步/兜底路径可能已并发收口，而
        # status-query 对「已终态」也返 False——复读一次，已收口即视为已处理。
        async with factory() as session:
            recheck = (
                await session.execute(
                    select(BankTxnOrder).where(
                        BankTxnOrder.tenant_id == tenant_id,
                        BankTxnOrder.biz_seq_no == biz_seq_no,
                    )
                )
            ).scalar_one_or_none()
        if recheck is not None and (
            recheck.finalized_at is not None or is_terminal(recheck.status)
        ):
            return
        raise CallbackTerminalUnresolved(
            f"callback without terminal txnStatus and status-query not converged "
            f"{tenant_id}/{biz_seq_no}"
        )

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
            if locked.finalized_at is not None or OrderStatus(locked.status) in ABSORBING_STATUSES:
                # 终态升级（reversal ingestion）：SUCCEEDED 已收口单收到 REVERSED 回调 =
                # counter 冲正（§3.6），合法升级——推进 + 状态级二次收口（audit/outbox
                # 按 REVERSED 键，不与首次 SUCCEEDED 收口互吞）。重复 REVERSED 回调时
                # status 已是 REVERSED → 落入下方幂等 return。FAILED 无资金动作不可冲正、
                # PARTIALLY_REVERSED 契约未定（wedap 4.8 阶段3）——均不升级。
                if (
                    OrderStatus(locked.status) == OrderStatus.SUCCEEDED
                    and terminal == OrderStatus.REVERSED
                ):
                    assert_transition(OrderStatus(locked.status), terminal)
                    locked.status = terminal
                    await finalize_terminal_in_session(
                        session,
                        order=locked,
                        source="CALLBACK",
                        trace_id=trace_id,
                        upgrade_from=str(OrderStatus.SUCCEEDED),
                    )
                    return
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
