import pytest
from sqlalchemy.exc import IntegrityError

from app.core.db import build_engine, build_session_factory
from app.models.base import Base
from app.models.callback import CallbackInbox, CallbackOutbox


@pytest.fixture()
async def session():
    engine = build_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with build_session_factory(engine)() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_inbox_unique_tenant_source_request(session) -> None:
    """同 (tenant_id, source, request_id) 重复插入应触发 IntegrityError。"""
    row = dict(
        tenant_id="t1", source="WEDAP_TXN", request_id="r1", payload={"a": 1}, status="RECEIVED"
    )
    session.add(CallbackInbox(**row))
    await session.commit()
    session.add(CallbackInbox(**row))
    with pytest.raises(IntegrityError):
        await session.commit()


@pytest.mark.asyncio
async def test_inbox_cross_tenant_no_conflict(session) -> None:
    """不同 tenant_id 相同 source+request_id 不冲突。"""
    base = dict(source="WEDAP_TXN", request_id="r1", payload={"a": 1})
    session.add(CallbackInbox(tenant_id="t1", **base))
    await session.commit()
    session.add(CallbackInbox(tenant_id="t2", **base))
    await session.commit()  # should not raise


@pytest.mark.asyncio
async def test_inbox_default_status(session) -> None:
    """inbox status 默认 RECEIVED。"""
    inbox = CallbackInbox(tenant_id="t1", source="WEDAP_TXN", request_id="r2", payload={})
    session.add(inbox)
    await session.commit()
    assert inbox.status == "RECEIVED"


@pytest.mark.asyncio
async def test_outbox_defaults(session) -> None:
    """outbox status 默认 PENDING，attempts 默认 0。"""
    ob = CallbackOutbox(tenant_id="t1", target="lifecycle", payload={"x": 1})
    session.add(ob)
    await session.commit()
    assert ob.status == "PENDING" and ob.attempts == 0
