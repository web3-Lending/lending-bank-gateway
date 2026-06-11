from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def build_engine(db_url: str) -> AsyncEngine:
    engine = create_async_engine(db_url, pool_pre_ping=True, pool_recycle=3600)
    if db_url.startswith("mysql"):  # pragma: no cover - 仅真 MySQL 路径

        @event.listens_for(engine.sync_engine, "connect")
        def _pin_utc(dbapi_conn, _record):  # type: ignore[no-untyped-def]
            with dbapi_conn.cursor() as cur:  # 规范11: DB session 钉 +00:00
                cur.execute("SET time_zone = '+00:00'")

    return engine


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
