import datetime as dt

import pytest

from app.core.db import build_engine, build_session_factory
from app.models.base import Base
from app.models.wedap_delivery import WedapImportDeliveryTask
from app.services.wedap_delivery import dispatch_delivery_once, enqueue_delivery

NOW = dt.datetime(2026, 6, 24, 0, 0, tzinfo=dt.UTC)

_KW = dict(
    tenant_id="WBTHK01",
    request_id="wedap-import-BATCH-LEN-20260624-001",
    import_batch_no="BATCH-LEN-20260624-001",
    data_type="interest-accrual",
    import_date="20260624",
    staging_key="staging/k.jsonl",
    file_checksum="a" * 64,
    file_size=128,
    total_count=3,
)


@pytest.fixture()
async def factory(tmp_path):
    engine = build_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield build_session_factory(engine)
    await engine.dispose()


async def _seed(factory, **over):
    async with factory() as s:
        await enqueue_delivery(s, **{**_KW, **over})
        await s.commit()


async def _get_task(factory, import_batch_no="BATCH-LEN-20260624-001"):
    async with factory() as s:
        from sqlalchemy import select

        return (
            await s.execute(
                select(WedapImportDeliveryTask).where(
                    WedapImportDeliveryTask.import_batch_no == import_batch_no
                )
            )
        ).scalar_one()


@pytest.mark.asyncio
async def test_dispatch_success_marks_delivered(factory):
    await _seed(factory)
    seen = []

    async def deliver(task):
        seen.append(task.import_batch_no)

    n = await dispatch_delivery_once(factory, deliver=deliver, now=NOW)

    assert n == 1
    assert seen == ["BATCH-LEN-20260624-001"]
    task = await _get_task(factory)
    assert task.status == "DELIVERED"
    # sqlite 丢 tzinfo（naive），按 naive 比对值
    assert task.notified_at.replace(tzinfo=None) == NOW.replace(tzinfo=None)
    assert task.attempts == 1
    assert task.last_error is None


@pytest.mark.asyncio
async def test_dispatch_transient_failure_reschedules(factory):
    await _seed(factory)

    async def deliver(task):
        raise RuntimeError("s3 timeout")

    await dispatch_delivery_once(factory, deliver=deliver, now=NOW, max_attempts=5, base_seconds=60)

    task = await _get_task(factory)
    assert task.status == "PENDING"  # 未达上限→回 PENDING 待重试
    assert task.attempts == 1
    assert task.next_retry_at.replace(tzinfo=None) == (NOW + dt.timedelta(seconds=60)).replace(
        tzinfo=None
    )
    assert "s3 timeout" in task.last_error


@pytest.mark.asyncio
async def test_dispatch_failure_at_max_attempts_marks_failed(factory):
    await _seed(factory)

    async def deliver(task):
        raise RuntimeError("notify 5xx")

    # max_attempts=1 → 第一次失败即终态 FAILED
    await dispatch_delivery_once(factory, deliver=deliver, now=NOW, max_attempts=1)

    task = await _get_task(factory)
    assert task.status == "FAILED"
    assert task.attempts == 1
    assert "notify 5xx" in task.last_error


@pytest.mark.asyncio
async def test_dispatch_skips_future_retry(factory):
    await _seed(factory)

    # 先失败一轮置 next_retry_at 未来
    async def fail(task):
        raise RuntimeError("x")

    await dispatch_delivery_once(factory, deliver=fail, now=NOW, base_seconds=600)
    # next_retry_at = NOW+600s；在 NOW+10s 再扫应跳过
    seen = []

    async def deliver(task):
        seen.append(task.id)

    n = await dispatch_delivery_once(factory, deliver=deliver, now=NOW + dt.timedelta(seconds=10))
    assert n == 0
    assert seen == []


@pytest.mark.asyncio
async def test_dispatch_skips_delivered(factory):
    await _seed(factory)

    async def ok(task):
        return None

    await dispatch_delivery_once(factory, deliver=ok, now=NOW)
    # 已 DELIVERED，再扫不重投
    n = await dispatch_delivery_once(factory, deliver=ok, now=NOW)
    assert n == 0


@pytest.mark.asyncio
async def test_dispatch_fires_on_terminal_for_delivered(factory):
    await _seed(factory)
    seen = []

    async def deliver(task):
        return None

    async def on_terminal(task, status, error):
        seen.append((task.import_batch_no, status, error))

    await dispatch_delivery_once(factory, deliver=deliver, now=NOW, on_terminal=on_terminal)
    assert seen == [("BATCH-LEN-20260624-001", "DELIVERED", None)]


@pytest.mark.asyncio
async def test_dispatch_fires_on_terminal_for_failed_at_max(factory):
    await _seed(factory)
    seen = []

    async def deliver(task):
        raise RuntimeError("notify 5xx")

    async def on_terminal(task, status, error):
        seen.append((status, error))

    await dispatch_delivery_once(
        factory, deliver=deliver, now=NOW, max_attempts=1, on_terminal=on_terminal
    )
    assert seen == [("FAILED", "notify 5xx")]


@pytest.mark.asyncio
async def test_dispatch_no_on_terminal_on_transient_retry(factory):
    await _seed(factory)
    seen = []

    async def deliver(task):
        raise RuntimeError("x")

    async def on_terminal(task, status, error):
        seen.append(status)

    # 未达上限→回 PENDING 待重试，不触发 on_terminal
    await dispatch_delivery_once(
        factory, deliver=deliver, now=NOW, max_attempts=5, on_terminal=on_terminal
    )
    assert seen == []
