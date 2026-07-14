from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.db import build_engine, build_session_factory
from app.models.base import Base
from app.models.txn import BankTxnOrder


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
