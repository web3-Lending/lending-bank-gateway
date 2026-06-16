"""集成测试（需 docker）：MySQL 8.0 下验证 audit hash-chain 的 per-tenant `FOR UPDATE`
串行化（A-m-003）——并发同 tenant 追加不分叉。

旧实现取链尾无锁，并发两笔同 tenant 读到同一 prev → 链分叉（森林）。修复后链尾 `FOR UPDATE`
串行化：后写者读到前写者已提交的新链尾再续链。

运行：.venv/bin/pytest -m integration --no-cov -q tests/integration/test_audit_chain_mysql.py
"""

from __future__ import annotations

import asyncio

import pytest
import sqlalchemy as sa

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def mysql_async_url() -> str:
    try:
        from testcontainers.mysql import MySqlContainer  # type: ignore[import-untyped]
    except ImportError:
        pytest.skip("testcontainers 未安装")
    try:
        import asyncmy  # noqa: F401
    except ImportError:
        pytest.skip("asyncmy 未安装")

    import app.models.audit  # noqa: F401  确保 AuditLog 注册到 Base.metadata
    from app.models.base import Base

    try:
        with MySqlContainer("mysql:8.0") as mysql:
            host = mysql.get_container_host_ip()
            port = mysql.get_exposed_port(3306)
            db = mysql.dbname
            root_sync = f"mysql+pymysql://root:test@{host}:{port}/{db}"
            sync_engine = sa.create_engine(root_sync, pool_pre_ping=True)
            with sync_engine.connect() as conn:
                conn.execute(sa.text("SET GLOBAL innodb_lock_wait_timeout = 5"))
                conn.commit()
            Base.metadata.create_all(sync_engine)
            sync_engine.dispose()
            yield f"mysql+asyncmy://root:test@{host}:{port}/{db}"
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"docker/testcontainer 不可用: {exc}")


async def _append(factory, tenant_id: str, action: str) -> None:
    from app.services.audit import write_audit

    async with factory() as session:
        async with session.begin():
            await write_audit(
                session,
                tenant_id=tenant_id,
                actor="svc:test",
                action=action,
                entity=f"e:{action}",
            )


@pytest.mark.asyncio
async def test_concurrent_same_tenant_audit_no_fork(mysql_async_url: str) -> None:
    """已有链 + 并发同 tenant 追加 → 链线性（无两行共享 prev_hash），无锁超时。"""
    from app.core.db import build_engine, build_session_factory
    from app.models.audit import AuditLog

    engine = build_engine(mysql_async_url)
    factory = build_session_factory(engine)
    tenant = "WBTHK-AUDIT-1"
    try:
        # 先建立非空链（首条）
        await _append(factory, tenant, "SEED")
        # 并发 5 笔同 tenant 追加
        await asyncio.gather(*[_append(factory, tenant, f"ACT-{i}") for i in range(5)])

        async with factory() as session:
            rows = (
                await session.execute(
                    sa.select(AuditLog.prev_hash, AuditLog.row_hash)
                    .where(AuditLog.tenant_id == tenant)
                    .order_by(AuditLog.id)
                )
            ).all()
        assert len(rows) == 6  # SEED + 5
        prevs = [r.prev_hash for r in rows]
        # 无分叉：任意两行不共享同一 prev_hash（每个 prev 唯一 → 线性链）
        assert len(prevs) == len(set(prevs)), f"audit chain forked: prevs={prevs}"
        # 每个非首节点的 prev_hash 必为链中某行的 row_hash（连续性）
        hashes = {r.row_hash for r in rows}
        from app.services.audit import GENESIS

        for r in rows:
            assert r.prev_hash == GENESIS or r.prev_hash in hashes
    finally:
        await engine.dispose()
