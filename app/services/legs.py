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

logger = logging.getLogger(__name__)

# 完全终态：不允许任何覆盖（REVERSED/FAILED/REVERSAL 不可改）。
# SUCCESS 可从 SUCCESS 推进到 REVERSED（合法冲正），故不列入完全终态集合；
# 但 SUCCESS→PENDING/UNKNOWN 等非终态倒退同样被拒（见下方 _ALLOWED_FROM_SUCCESS）。
_HARD_TERMINAL_LEG_STATUSES: frozenset[str] = frozenset(
    {LegStatus.REVERSED, LegStatus.FAILED, LegStatus.REVERSAL}
)

# SUCCESS 只允许向 REVERSED 推进（冲正），其余变更视为非法倒退
_ALLOWED_FROM_SUCCESS: frozenset[str] = frozenset({LegStatus.SUCCESS, LegStatus.REVERSED})


async def sync_legs_for(
    factory: async_sessionmaker[AsyncSession],
    *,
    wedap: Any,
    tenant_id: str,
    biz_seq_no: str,
) -> None:
    """拉 steps → upsert leg（external_ref/金额不可变，status 可推进）→ 聚合父单。

    upsert key: (tenant, biz_seq, step_seq)。
    防御：聚合 ValueError / IllegalTransition 不上抛（log+留待下次回调/重放收敛），
    保证回调摄取不被单笔脏数据阻塞。

    注意：sync_legs_for 与 inbox status 推进为两事务（至少一次语义），崩溃窗口由重放再
    驱动收敛。
    """
    steps = await wedap.get_composite_steps(tenant_id=tenant_id, biz_seq_no=biz_seq_no)
    async with factory() as session:
        async with session.begin():
            order = (
                await session.execute(
                    select(BankTxnOrder).where(
                        BankTxnOrder.tenant_id == tenant_id,
                        BankTxnOrder.biz_seq_no == biz_seq_no,
                    )
                )
            ).scalar_one_or_none()
            if order is None:
                logger.warning("sync_legs: no order %s/%s", tenant_id, biz_seq_no)
                return
            existing = {
                leg.step_seq: leg
                for leg in (
                    await session.execute(
                        select(BankTxnLeg).where(
                            BankTxnLeg.tenant_id == tenant_id,
                            BankTxnLeg.biz_seq_no == biz_seq_no,
                        )
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
                    ) or (
                        leg.status == LegStatus.SUCCESS and new_status not in _ALLOWED_FROM_SUCCESS
                    )
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
                return
            try:
                new_status = aggregate_order_status(
                    [(LegStatus(leg.status), str(leg.amount)) for leg in all_legs]
                )
                if new_status != OrderStatus(order.status):
                    assert_transition(OrderStatus(order.status), new_status)
                    order.status = new_status
            except (ValueError, IllegalTransition):
                logger.exception(
                    "sync_legs aggregate/transition rejected %s/%s",
                    tenant_id,
                    biz_seq_no,
                )
