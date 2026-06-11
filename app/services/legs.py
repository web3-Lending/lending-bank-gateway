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
                    existing[seq].status = str(s["status"])
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
