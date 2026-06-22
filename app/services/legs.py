import logging
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.states import (
    IllegalTransition,
    LegStatus,
    OrderStatus,
    aggregate_order_status,
    assert_transition,
)
from app.models.txn import BankTxnLeg, BankTxnOrder
from app.services.order_finalize import finalize_terminal_in_session, is_terminal


class LegsSyncIncomplete(Exception):
    """legs 同步过程中聚合或状态转移失败，摄取链必须让 inbox 留 RECEIVED 等重放收敛。

    抛出此异常意味着本次 sync_legs_for 执行未能完成聚合/转移，但已落库的 leg 行仍保留。
    调用方（_after_ingest）捕获后应中断后续 outbox enqueue，让 inbox 行留 RECEIVED，
    由重放机制驱动最终收敛。
    """


logger = logging.getLogger(__name__)

# 完全终态：不允许任何覆盖（REVERSED/FAILED/REVERSAL 不可改）。
# SUCCESS 可从 SUCCESS 推进到 REVERSED（合法冲正），故不列入完全终态集合；
# 但 SUCCESS→PENDING/UNKNOWN 等非终态倒退同样被拒（见下方 _ALLOWED_FROM_SUCCESS）。
_HARD_TERMINAL_LEG_STATUSES: frozenset[str] = frozenset(
    {LegStatus.REVERSED, LegStatus.FAILED, LegStatus.REVERSAL}
)

# SUCCESS 只允许向 REVERSED 推进（冲正），其余变更视为非法倒退
_ALLOWED_FROM_SUCCESS: frozenset[str] = frozenset({LegStatus.SUCCESS, LegStatus.REVERSED})


async def apply_legs_in_session(
    session: AsyncSession,
    *,
    tenant_id: str,
    biz_seq_no: str,
    steps: list[dict[str, Any]],
    source: str = "CALLBACK",
    trace_id: str = "",
) -> None:
    """在**调用方事务内** upsert leg（external_ref/金额不可变，status 可推进）→ 聚合父单。

    steps 由调用方在**事务外**预取（外呼 get_composite_steps），本函数只做 DB 写，故可与
    outbox enqueue 等其它写操作纳入同一事务原子提交（A-C-002）。
    upsert key: (tenant, biz_seq, step_seq)。

    并发安全：order 行和 legs 行均加 FOR UPDATE 行锁，串行化并发 sync。SQLite 忽略该子句。

    失败语义：
    - order 不存在（未知 tenant/biz_seq_no、乱序回调）→ 抛 LegsSyncIncomplete，调用方据此中断
      后续 enqueue，inbox 留 RECEIVED 待重放补偿。
    - 聚合 ValueError / IllegalTransition → 同上，抛 LegsSyncIncomplete。
    """
    # FOR UPDATE：并发 sync 串行化，防两个 goroutine 各自聚合写入冲突
    order = (
        await session.execute(
            select(BankTxnOrder)
            .where(
                BankTxnOrder.tenant_id == tenant_id,
                BankTxnOrder.biz_seq_no == biz_seq_no,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if order is None:
        raise LegsSyncIncomplete(f"unknown order {tenant_id}/{biz_seq_no}")
    # FOR UPDATE：锁定所有关联 leg 行，防并发 upsert 互相覆盖
    existing = {
        leg.step_seq: leg
        for leg in (
            await session.execute(
                select(BankTxnLeg)
                .where(
                    BankTxnLeg.tenant_id == tenant_id,
                    BankTxnLeg.biz_seq_no == biz_seq_no,
                )
                .with_for_update()
            )
        ).scalars()
    }
    for s in steps:
        seq = int(s["stepSeq"])
        if seq in existing:
            leg = existing[seq]
            new_status = str(s["status"])
            # ref 漂移告警：external_ref 不可变，不同时只告警不修改
            if str(s["sysRefNo"]) != leg.external_ref:
                logger.warning(
                    "external_ref drift: seq=%s old=%s new=%s",
                    seq,
                    leg.external_ref,
                    s["sysRefNo"],
                )
            # 终态防倒退：完全终态不可改；SUCCESS 只允许向 REVERSED 推进
            is_terminal_overwrite = (
                leg.status in _HARD_TERMINAL_LEG_STATUSES and new_status != leg.status
            ) or (leg.status == LegStatus.SUCCESS and new_status not in _ALLOWED_FROM_SUCCESS)
            if is_terminal_overwrite:
                logger.warning(
                    "terminal leg status overwrite rejected: seq=%s status=%s",
                    seq,
                    leg.status,
                )
            else:
                leg.status = new_status
        else:
            session.add(
                BankTxnLeg(
                    tenant_id=tenant_id,
                    order_id=order.id,
                    biz_seq_no=biz_seq_no,
                    external_system="WEDAP_BANK",
                    external_ref=str(s["sysRefNo"]),
                    step_type=str(s["stepType"]),
                    step_seq=seq,
                    amount=Decimal(str(s["amount"])),
                    currency=str(s.get("currencyCode", "USD")),
                    payer_account=s.get("payerAccount"),
                    payee_account=s.get("payeeAccount"),
                    status=str(s["status"]),
                    txn_date=s.get("txnDate"),
                )
            )
    await session.flush()
    all_legs = (
        (
            await session.execute(
                select(BankTxnLeg).where(
                    BankTxnLeg.tenant_id == tenant_id,
                    BankTxnLeg.biz_seq_no == biz_seq_no,
                )
            )
        )
        .scalars()
        .all()
    )
    if not all_legs:
        # 空 steps 不静默 no-op（codex HIGH-3）：order 终态但 legs 缺失（如 CLT 无 composite
        # 明细）会留资金流水裂缝，必须留痕告警，由后续重放/兜底 worker 补拉，而非吞掉。
        logger.warning(
            "apply_legs: no legs for %s/%s (source=%s); ledger 明细缺失待补拉",
            tenant_id,
            biz_seq_no,
            source,
        )
        return
    try:
        new_status = aggregate_order_status(
            [(LegStatus(leg.status), str(leg.amount)) for leg in all_legs]
        )
        if new_status != OrderStatus(order.status):
            assert_transition(OrderStatus(order.status), new_status)
            order.status = new_status
            # 终态统一收口：finalized_at/via + audit + 转发 lifecycle（稳定 key）。
            # 幂等：若同步路径已收口（finalized_at 非空）则 finalize 内部跳过，不重复转发。
            if is_terminal(new_status):
                await finalize_terminal_in_session(
                    session, order=order, source=source, trace_id=trace_id
                )
    except (ValueError, IllegalTransition) as exc:
        logger.exception(
            "sync_legs aggregate/transition rejected %s/%s",
            tenant_id,
            biz_seq_no,
        )
        raise LegsSyncIncomplete(
            f"sync_legs aggregate/transition failed for {tenant_id}/{biz_seq_no}"
        ) from exc


async def sync_legs_for(
    factory: async_sessionmaker[AsyncSession],
    *,
    wedap: Any,
    tenant_id: str,
    biz_seq_no: str,
    source: str = "RECONCILE",
) -> None:
    """拉 steps（事务外外呼）→ 单事务 apply_legs_in_session。order-reconcile worker 兜底入口。"""
    steps = await wedap.get_composite_steps(tenant_id=tenant_id, biz_seq_no=biz_seq_no)
    async with factory() as session:
        async with session.begin():
            await apply_legs_in_session(
                session,
                tenant_id=tenant_id,
                biz_seq_no=biz_seq_no,
                steps=steps,
                source=source,
            )
