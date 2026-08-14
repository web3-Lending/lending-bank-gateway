"""集成测试（需 docker）：MySQL 8.0 下验证 outbox 原子 claim（A-M-001）——并发两个 dispatcher
不重复投递、attempts 不双计。

旧实现快照读后无 claim，多副本同轮各投一次 + attempts 各 +1。修复后每行先条件 UPDATE 置 SENDING
（rowcount==1 才拥有），两副本同行只有一个 claim 成功。

运行：.venv/bin/pytest -m integration --no-cov -q tests/integration/test_outbox_claim_mysql.py
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx
import sqlalchemy as sa

pytestmark = pytest.mark.integration

TARGETS = {"lifecycle": "http://lifecycle/cb"}


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

    import app.models.callback  # noqa: F401
    from app.models.base import Base

    try:
        with MySqlContainer("mysql:8.0").with_command("--innodb-use-native-aio=0") as mysql:
            host = mysql.get_container_host_ip()
            port = mysql.get_exposed_port(3306)
            db = mysql.dbname
            sync_engine = sa.create_engine(
                f"mysql+pymysql://root:test@{host}:{port}/{db}", pool_pre_ping=True
            )
            Base.metadata.create_all(sync_engine)
            sync_engine.dispose()
            yield f"mysql+asyncmy://root:test@{host}:{port}/{db}"
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"docker/testcontainer 不可用: {exc}")


@pytest.mark.asyncio
@respx.mock
async def test_concurrent_dispatchers_no_double_delivery(mysql_async_url: str) -> None:
    """10 行 + 两 dispatcher 并发投递 → 每行恰好 SENT 一次、attempts==1（无重复投递/双计）。"""
    from app.core.db import build_engine, build_session_factory
    from app.models.callback import CallbackOutbox
    from app.services.outbox import dispatch_once, enqueue_forward

    respx.post("http://lifecycle/cb").mock(return_value=httpx.Response(200))

    engine = build_engine(mysql_async_url)
    factory = build_session_factory(engine)
    try:
        # 清表 + 入队 10 条 PENDING
        async with factory() as s:
            async with s.begin():
                await s.execute(sa.delete(CallbackOutbox))
        async with factory() as s:
            async with s.begin():
                for i in range(10):
                    await enqueue_forward(
                        s,
                        tenant_id="OCBC",
                        target="lifecycle",
                        payload={"bizSeqNo": f"B{i}"},
                        dedup_key=f"fwd-claim-{i}",
                    )

        # 两个 dispatcher 并发跑
        results = await asyncio.gather(
            dispatch_once(factory, targets=TARGETS, max_attempts=3),
            dispatch_once(factory, targets=TARGETS, max_attempts=3),
        )

        async with factory() as s:
            rows = (await s.execute(sa.select(CallbackOutbox))).scalars().all()
        assert len(rows) == 10
        # 每行恰好被处理一次：SENT 且 attempts==1（无双计=无重复投递）
        for r in rows:
            assert r.status == "SENT", f"row {r.id} status={r.status}"
            assert r.attempts == 1, f"row {r.id} attempts={r.attempts} (双计=重复投递)"
        # 两 dispatcher 合计恰好处理 10 行
        assert sum(results) == 10
    finally:
        await engine.dispose()
