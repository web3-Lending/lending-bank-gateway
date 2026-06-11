import pytest
from sqlalchemy import select

from app.core.db import build_engine, build_session_factory
from app.models.audit import AuditLog
from app.models.base import Base
from app.services.audit import write_audit


@pytest.fixture()
async def factory():
    engine = build_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield build_session_factory(engine)
    await engine.dispose()


@pytest.mark.asyncio
async def test_hash_chain_links(factory) -> None:
    async with factory() as s:
        async with s.begin():
            await write_audit(
                s,
                tenant_id="t1",
                actor="svc:gateway",
                action="ORDER_SUBMITTED",
                entity="bank_txn_order:1",
                payload={"s": "SUBMITTED"},
            )
            await write_audit(
                s,
                tenant_id="t1",
                actor="svc:gateway",
                action="ORDER_FINALIZED",
                entity="bank_txn_order:1",
                payload={"s": "SUCCEEDED"},
            )
    async with factory() as s:
        rows = (await s.execute(select(AuditLog).order_by(AuditLog.id))).scalars().all()
        assert rows[0].prev_hash == "0" * 64
        assert rows[1].prev_hash == rows[0].row_hash


@pytest.mark.asyncio
async def test_hash_chain_per_tenant(factory) -> None:
    async with factory() as s:
        async with s.begin():
            await write_audit(s, tenant_id="t1", actor="a", action="X", entity="e:1")
            await write_audit(s, tenant_id="t2", actor="a", action="X", entity="e:1")
    async with factory() as s:
        rows = (await s.execute(select(AuditLog).order_by(AuditLog.id))).scalars().all()
        assert rows[1].prev_hash == "0" * 64  # t2 链独立，从 GENESIS 起


@pytest.mark.asyncio
async def test_row_hash_deterministic_and_chained(factory) -> None:
    async with factory() as s:
        async with s.begin():
            await write_audit(
                s, tenant_id="t1", actor="a", action="X", entity="e:1", payload={"k": "v"}
            )
    async with factory() as s:
        row = (await s.execute(select(AuditLog))).scalar_one()
        import hashlib
        import json

        canonical = json.dumps(
            {
                "tenant": "t1",
                "actor": "a",
                "action": "X",
                "entity": "e:1",
                "payload": {"k": "v"},
                "prev": "0" * 64,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        assert row.row_hash == hashlib.sha256(canonical.encode()).hexdigest()
