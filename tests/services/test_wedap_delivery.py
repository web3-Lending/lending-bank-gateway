import datetime as dt

import pytest

from app.core.db import build_engine, build_session_factory
from app.models.base import Base
from app.services.wedap_delivery import compute_next_retry, enqueue_delivery


@pytest.fixture()
async def session():
    engine = build_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with build_session_factory(engine)() as s:
        yield s
    await engine.dispose()


_KW = dict(
    tenant_id="WBTHK01",
    request_id="wedap-import-BATCH-LEN-20260624-001",
    import_batch_no="BATCH-LEN-20260624-001",
    data_type="interest-accrual",
    import_date="20260624",
    staging_key="staging/20260624/interest-accrual.jsonl",
    file_checksum="a" * 64,
    file_size=128,
    total_count=3,
)


@pytest.mark.asyncio
async def test_enqueue_inserts_pending_task(session):
    task = await enqueue_delivery(session, **_KW)
    await session.commit()

    assert task.id is not None
    assert task.status == "PENDING"
    assert task.import_batch_no == "BATCH-LEN-20260624-001"
    assert task.request_id == "wedap-import-BATCH-LEN-20260624-001"
    assert task.file_checksum == "a" * 64
    assert task.total_count == 3
    assert task.attempts == 0


@pytest.mark.asyncio
async def test_enqueue_idempotent_returns_existing(session):
    first = await enqueue_delivery(session, **_KW)
    await session.commit()
    second = await enqueue_delivery(session, **_KW)
    await session.commit()

    assert first.id == second.id  # 同 import_batch_no → 返回既有任务，不重插


@pytest.mark.asyncio
async def test_compute_next_retry_exponential():
    now = dt.datetime(2026, 6, 24, 0, 0, tzinfo=dt.UTC)
    assert compute_next_retry(now, 1, base_seconds=60) == now + dt.timedelta(seconds=60)
    assert compute_next_retry(now, 2, base_seconds=60) == now + dt.timedelta(seconds=120)
    assert compute_next_retry(now, 3, base_seconds=60) == now + dt.timedelta(seconds=240)
    # attempts=0 退化为 base（2^0）
    assert compute_next_retry(now, 0, base_seconds=60) == now + dt.timedelta(seconds=60)
