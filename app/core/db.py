from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def _pool_kwargs(db_url: str, pool_size: int, max_overflow: int) -> dict[str, int]:
    """返回适合该 db_url 的连接池参数。

    sqlite 使用 StaticPool/NullPool 体系，不支持 pool_size / max_overflow；
    其它方言（MySQL 等）透传这两个参数。
    """
    if db_url.startswith("sqlite"):
        return {}
    return {"pool_size": pool_size, "max_overflow": max_overflow}


def build_engine(
    db_url: str,
    *,
    pool_size: int = 5,
    max_overflow: int = 10,
) -> AsyncEngine:
    kwargs = _pool_kwargs(db_url, pool_size, max_overflow)
    engine = create_async_engine(db_url, pool_pre_ping=True, pool_recycle=3600, **kwargs)
    if db_url.startswith("mysql"):  # pragma: no cover - 仅真 MySQL 路径

        @event.listens_for(engine.sync_engine, "connect")
        def _pin_utc(dbapi_conn, _record):  # type: ignore[no-untyped-def]
            with dbapi_conn.cursor() as cur:  # 规范11: DB session 钉 +00:00
                cur.execute("SET time_zone = '+00:00'")

    if db_url.startswith("sqlite"):  # pragma: no branch
        # SQLite 默认关闭外键约束；启用后复合 FK（leg→order 跨租户保护）才生效。
        # 注：aiosqlite 在异步引擎下通过 sync_engine 事件注入 PRAGMA。
        @event.listens_for(engine.sync_engine, "connect")
        def _enable_fk(dbapi_conn, _record):  # type: ignore[no-untyped-def]
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

    return engine


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """构造异步 session 工厂。

    调用方必须 ``async with factory() as session`` 使用；
    裸调 factory() 不关闭会泄漏连接。
    """
    return async_sessionmaker(engine, expire_on_commit=False)
