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


@pytest.mark.asyncio
async def test_anchor_seeds_from_existing_history_not_genesis(factory) -> None:
    """M1：锚点缺失但已有 audit_log 历史时，write_audit 必须从历史链尾接续，而非 GENESIS 断链。

    模拟「升级前已有审计历史、audit_chain_head 尚未回填」的库：直接插一条历史 audit_log，
    不建锚点；write_audit 应 seed 锚点为历史链尾 row_hash，新行 prev_hash == 历史行 row_hash。
    """
    from app.models.audit import AuditChainHead

    # 直接插入一条「历史」audit_log（无锚点），模拟升级前数据
    async with factory() as s:
        async with s.begin():
            hist = AuditLog(
                tenant_id="t1",
                actor="svc:legacy",
                action="LEGACY",
                entity="e:0",
                prev_hash="0" * 64,
                row_hash="a" * 64,
            )
            s.add(hist)
    # 确认此时无锚点
    async with factory() as s:
        assert (await s.get(AuditChainHead, "t1")) is None

    # write_audit：应从历史链尾 ("a"*64) 接续，而非 GENESIS
    async with factory() as s:
        async with s.begin():
            await write_audit(s, tenant_id="t1", actor="svc:gw", action="NEW", entity="e:1")

    async with factory() as s:
        rows = (await s.execute(select(AuditLog).order_by(AuditLog.id))).scalars().all()
        new_row = rows[-1]
        assert new_row.prev_hash == "a" * 64, "新行应接历史链尾，而非 GENESIS（M1 断链）"
        anchor = await s.get(AuditChainHead, "t1")
        assert anchor is not None and anchor.last_row_hash == new_row.row_hash
