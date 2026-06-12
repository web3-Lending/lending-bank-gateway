import pytest
from sqlalchemy import text

from app.core.config import Settings
from app.core.db import _pool_kwargs, build_engine, build_session_factory


@pytest.mark.asyncio
async def test_session_roundtrip() -> None:
    engine = build_engine("sqlite+aiosqlite:///:memory:")
    factory = build_session_factory(engine)
    async with factory() as session:
        assert (await session.execute(text("SELECT 1"))).scalar() == 1
    await engine.dispose()


def test_pool_kwargs_sqlite_empty() -> None:
    """sqlite 路径不应携带 pool_size / max_overflow（StaticPool 体系不支持）。"""
    assert _pool_kwargs("sqlite+aiosqlite:///:memory:", 5, 10) == {}


def test_pool_kwargs_mysql_passthrough() -> None:
    """非 sqlite 方言应透传 pool_size / max_overflow。"""
    result = _pool_kwargs("mysql+aiomysql://user:pass@host/db", 3, 7)
    assert result == {"pool_size": 3, "max_overflow": 7}


@pytest.mark.asyncio
async def test_build_engine_sqlite_still_works() -> None:
    """传入 pool_size 参数时 sqlite 路径仍能正常建 engine。"""
    engine = build_engine("sqlite+aiosqlite:///:memory:", pool_size=3, max_overflow=6)
    factory = build_session_factory(engine)
    async with factory() as session:
        assert (await session.execute(text("SELECT 1"))).scalar() == 1
    await engine.dispose()


def test_expire_on_commit_false() -> None:
    """钉死设计决策：expire_on_commit 必须为 False。"""
    engine = build_engine("sqlite+aiosqlite:///:memory:")
    factory = build_session_factory(engine)
    assert factory.kw["expire_on_commit"] is False


def test_settings_pool_defaults() -> None:
    """Settings 新字段默认值符合规格。"""
    s = Settings()
    assert s.db_pool_size == 5
    assert s.db_max_overflow == 10
