import pytest
from sqlalchemy import text

from app.core.db import build_engine, build_session_factory


@pytest.mark.asyncio
async def test_session_roundtrip() -> None:
    engine = build_engine("sqlite+aiosqlite:///:memory:")
    factory = build_session_factory(engine)
    async with factory() as session:
        assert (await session.execute(text("SELECT 1"))).scalar() == 1
    await engine.dispose()
