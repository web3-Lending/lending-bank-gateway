import datetime as dt
from unittest.mock import AsyncMock

import pytest

from app.core.db import build_engine, build_session_factory
from app.models.base import Base
from app.models.wedap_delivery import WedapImportDeliveryTask
from app.services import wedap_delivery as _mod
from app.services.wedap_delivery import (
    _claim,
    _reclaim_stale_sending,
    dispatch_delivery_once,
    enqueue_delivery,
)

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


async def _insert(factory, *, status, locked_at=None, import_batch_no="BATCH-LEN-20260624-902"):
    async with factory() as s:
        s.add(
            WedapImportDeliveryTask(
                tenant_id="WBTHK01",
                request_id=f"wedap-import-{import_batch_no}",
                import_batch_no=import_batch_no,
                data_type="interest-accrual",
                import_date="20260624",
                staging_key="k",
                file_checksum="a" * 64,
                file_size=1,
                total_count=1,
                status=status,
                locked_at=locked_at,
            )
        )
        await s.commit()


@pytest.mark.asyncio
async def test_claim_atomic_second_claim_loses(factory):
    """原子 claim：第一次 PENDING→SENDING 成功，第二次(已 SENDING)失败。"""
    await _insert(factory, status="PENDING")
    task = await _get_task(factory, "BATCH-LEN-20260624-902")
    assert await _claim(factory, task.id, NOW) is True
    assert (await _get_task(factory, "BATCH-LEN-20260624-902")).status == "SENDING"
    assert await _claim(factory, task.id, NOW) is False  # 已被抢


@pytest.mark.asyncio
async def test_dispatch_skips_when_claim_lost(factory, monkeypatch):
    """claim 抢不到(别的副本先抢)→ 跳过,不投递。"""
    await _seed(factory)
    monkeypatch.setattr(_mod, "_claim", AsyncMock(return_value=False))
    called = []

    async def deliver(task):
        called.append(task.import_batch_no)

    n = await dispatch_delivery_once(factory, deliver=deliver, now=NOW)
    assert n == 0
    assert called == []  # 没抢到不投递


@pytest.mark.asyncio
async def test_reclaim_stale_sending_back_to_pending(factory):
    """崩溃残留 SENDING(locked_at 超时)→ reclaim 回 PENDING。"""
    stale = NOW - dt.timedelta(seconds=3600)
    await _insert(factory, status="SENDING", locked_at=stale)
    await _reclaim_stale_sending(factory, now=NOW, claim_timeout_seconds=300)
    row = await _get_task(factory, "BATCH-LEN-20260624-902")
    assert row.status == "PENDING"
    assert row.locked_at is None


@pytest.mark.asyncio
async def test_reclaim_keeps_fresh_sending(factory):
    """新鲜 SENDING(未超时)→ 不 reclaim。"""
    fresh = NOW - dt.timedelta(seconds=10)
    await _insert(factory, status="SENDING", locked_at=fresh)
    await _reclaim_stale_sending(factory, now=NOW, claim_timeout_seconds=300)
    assert (await _get_task(factory, "BATCH-LEN-20260624-902")).status == "SENDING"
