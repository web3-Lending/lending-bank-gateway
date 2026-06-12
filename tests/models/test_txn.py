from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.db import build_engine, build_session_factory
from app.models.base import Base
from app.models.txn import BankTxnLeg, BankTxnOrder


@pytest.fixture()
async def session():
    engine = build_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with build_session_factory(engine)() as s:
        yield s
    await engine.dispose()


def _order(**kw) -> BankTxnOrder:
    defaults = dict(
        tenant_id="WBTHK01",
        biz_seq_no="DSB-20260611-0001234567890",
        business_action="DISBURSE",
        biz_type="DSB",
        amount=Decimal("100.0000"),
        currency="USD",
        caller_service="lifecycle",
        status="ACCEPTED",
    )
    defaults.update(kw)
    return BankTxnOrder(**defaults)


@pytest.mark.asyncio
async def test_order_unique_tenant_biz_seq_no(session) -> None:
    """同 (tenant_id, biz_seq_no) 重复插入应触发 IntegrityError。"""
    session.add(_order())
    await session.commit()
    session.add(_order())
    with pytest.raises(IntegrityError):
        await session.commit()


@pytest.mark.asyncio
async def test_leg_unique_external_ref_and_step_seq(session) -> None:
    """uq_leg_tenant_ext: 同 (tenant_id, external_system, external_ref) 重复触发 IntegrityError。"""
    order = _order()
    session.add(order)
    await session.commit()
    leg = dict(
        tenant_id="WBTHK01",
        order_id=order.id,
        biz_seq_no=order.biz_seq_no,
        external_system="WEDAP_BANK",
        step_type="DISBURSEMENT_COLLECTION",
        step_seq=1,
        external_ref="HSBC202606110001",
        amount=Decimal("100.0000"),
        currency="USD",
        status="SUCCESS",
    )
    session.add(BankTxnLeg(**leg))
    await session.commit()
    # 同 external_ref 换 step_seq=2 → 仍撞 uq_leg_tenant_ext
    session.add(BankTxnLeg(**{**leg, "step_seq": 2}))
    with pytest.raises(IntegrityError):
        await session.commit()


@pytest.mark.asyncio
async def test_leg_unique_step_seq(session) -> None:
    """同 (tenant_id, biz_seq_no, step_seq) 重复——uq_leg_tenant_biz_step 应触发 IntegrityError。"""
    order = _order()
    session.add(order)
    await session.commit()
    leg_base = dict(
        tenant_id="WBTHK01",
        order_id=order.id,
        biz_seq_no=order.biz_seq_no,
        external_system="WEDAP_BANK",
        step_type="DISBURSEMENT_COLLECTION",
        step_seq=1,
        amount=Decimal("100.0000"),
        currency="USD",
        status="SUCCESS",
    )
    session.add(BankTxnLeg(**{**leg_base, "external_ref": "HSBC202606110001"}))
    await session.commit()
    # 不同 external_ref 但相同 step_seq=1 → 撞 uq_leg_tenant_biz_step
    session.add(BankTxnLeg(**{**leg_base, "external_ref": "HSBC202606110002"}))
    with pytest.raises(IntegrityError):
        await session.commit()


@pytest.mark.asyncio
async def test_leg_cross_tenant_order_ref_rejected(session) -> None:
    """leg 跨租户引用 order（order.tenant_id != leg.tenant_id）应触发 IntegrityError。

    复合 FK fk_leg_order_tenant: leg.(order_id, tenant_id) → order.(id, tenant_id)。
    SQLite 需要 PRAGMA foreign_keys=ON（由 build_engine sqlite 路径自动设置）。
    """
    order_t1 = _order(tenant_id="TENANT-A", biz_seq_no="DSB-A-001")
    session.add(order_t1)
    await session.commit()

    # leg.tenant_id="TENANT-B" 但 order_id 指向 TENANT-A 的 order
    # 复合 FK 要求 (order_id, tenant_id) 必须都匹配 → 应触发 IntegrityError
    cross_leg = BankTxnLeg(
        tenant_id="TENANT-B",
        order_id=order_t1.id,  # 指向不同 tenant 的 order
        biz_seq_no="DSB-B-001",
        external_system="WEDAP_BANK",
        external_ref="HSBC-CROSS-0001",
        step_type="DISBURSEMENT_COLLECTION",
        step_seq=1,
        amount=Decimal("50.0000"),
        currency="USD",
        status="PENDING",
    )
    session.add(cross_leg)
    with pytest.raises(IntegrityError):
        await session.commit()
