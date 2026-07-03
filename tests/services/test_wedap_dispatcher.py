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

    n = await dispatch_delivery_once(factory, deliver=deliver, now=NOW, clock=lambda: NOW)

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


# ─────────────────────── §6.1 五护栏（护栏②③④） ───────────────────────

from app.models.wedap_delivery_alert import WedapDeliveryAlert  # noqa: E402
from app.services.wedap_delivery import (  # noqa: E402
    alert_stuck_deliveries,
    compute_result_deadline,
)

_DEADLINE = dt.datetime(2026, 6, 25, 2, 30, tzinfo=dt.UTC)


@pytest.mark.asyncio
async def test_dispatch_delivered_records_guardrail_fields(factory):
    """护栏②：受理成功落 accepted_at / result_file_path / result_deadline_at。"""
    await _seed(factory)

    async def deliver(task):
        return {"status": "ACCEPTED", "resultFilePath": "lending/import/x/_result.json"}

    n = await dispatch_delivery_once(
        factory, deliver=deliver, now=NOW, result_deadline=lambda now: _DEADLINE
    )
    assert n == 1
    task = await _get_task(factory)
    assert task.status == "DELIVERED"
    assert task.accepted_at is not None
    assert task.result_file_path == "lending/import/x/_result.json"
    assert task.result_deadline_at is not None
    assert task.result_deadline_at.replace(tzinfo=dt.UTC) == _DEADLINE


@pytest.mark.asyncio
async def test_dispatch_delivered_none_response_still_accepts(factory):
    """deliver 返回 None（无响应体）→ accepted_at 仍置，path/deadline 留空不崩。"""
    await _seed(factory)

    async def deliver(task):
        return None

    await dispatch_delivery_once(factory, deliver=deliver, now=NOW)
    task = await _get_task(factory)
    assert task.status == "DELIVERED"
    assert task.accepted_at is not None
    assert task.result_file_path is None
    assert task.result_deadline_at is None


@pytest.mark.asyncio
async def test_dispatch_non_str_resultfilepath_ignored(factory):
    """resultFilePath 非 str（wedap 回脏值）→ 落 None，不带毒进库。"""
    await _seed(factory)

    async def deliver(task):
        return {"status": "ACCEPTED", "resultFilePath": 12345}

    await dispatch_delivery_once(factory, deliver=deliver, now=NOW)
    task = await _get_task(factory)
    assert task.result_file_path is None


def test_compute_result_deadline_before_anchor():
    """当日 anchor 未到 → 当日 anchor + grace。"""
    now = dt.datetime(2026, 6, 24, 1, 0, tzinfo=dt.UTC)
    got = compute_result_deadline(now, anchor_hour=2, grace_minutes=30)
    assert got == dt.datetime(2026, 6, 24, 2, 30, tzinfo=dt.UTC)


def test_compute_result_deadline_after_anchor():
    """当日 anchor 已过 → 次日 anchor + grace。"""
    now = dt.datetime(2026, 6, 24, 3, 0, tzinfo=dt.UTC)
    got = compute_result_deadline(now, anchor_hour=2, grace_minutes=30)
    assert got == dt.datetime(2026, 6, 25, 2, 30, tzinfo=dt.UTC)


def test_compute_result_deadline_at_anchor_takes_next_day():
    """正好落在 anchor 时刻 → 保守取次日窗口。"""
    now = dt.datetime(2026, 6, 24, 2, 0, tzinfo=dt.UTC)
    got = compute_result_deadline(now, anchor_hour=2, grace_minutes=30)
    assert got == dt.datetime(2026, 6, 25, 2, 30, tzinfo=dt.UTC)


async def _get_alerts(factory):
    async with factory() as s:
        from sqlalchemy import select

        return list((await s.execute(select(WedapDeliveryAlert))).scalars().all())


@pytest.mark.asyncio
async def test_alert_pending_stuck_dedup(factory):
    """护栏③：PENDING 超龄 → 告警一次；重复扫描不再新增（唯一约束去重）。"""
    await _seed(factory)  # created_at = 现在（sqlite server_default）
    later = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=3600)

    first = await alert_stuck_deliveries(
        factory, now=later, pending_max_age_seconds=1800.0, batch_limit=100
    )
    again = await alert_stuck_deliveries(
        factory, now=later, pending_max_age_seconds=1800.0, batch_limit=100
    )
    assert first == 1
    assert again == 0
    alerts = await _get_alerts(factory)
    assert len(alerts) == 1
    assert alerts[0].kind == "PENDING_STUCK"
    assert alerts[0].import_batch_no == "BATCH-LEN-20260624-001"
    assert "status=PENDING" in alerts[0].detail


@pytest.mark.asyncio
async def test_alert_pending_fresh_not_alerted(factory):
    """未超龄的 PENDING 不告警。"""
    await _seed(factory)
    n = await alert_stuck_deliveries(
        factory,
        now=dt.datetime.now(dt.UTC),
        pending_max_age_seconds=1800.0,
        batch_limit=100,
    )
    assert n == 0
    assert await _get_alerts(factory) == []


@pytest.mark.asyncio
async def test_alert_result_overdue(factory):
    """护栏④：DELIVERED 超截止未回收 → RESULT_OVERDUE 告警。"""
    await _seed(factory)

    async def deliver(task):
        return {"status": "ACCEPTED"}

    await dispatch_delivery_once(
        factory, deliver=deliver, now=NOW, result_deadline=lambda now: _DEADLINE
    )
    after_deadline = _DEADLINE + dt.timedelta(minutes=1)
    n = await alert_stuck_deliveries(
        factory, now=after_deadline, pending_max_age_seconds=86400.0, batch_limit=100
    )
    assert n == 1
    alerts = await _get_alerts(factory)
    assert alerts[0].kind == "RESULT_OVERDUE"
    assert "deadline=" in alerts[0].detail


@pytest.mark.asyncio
async def test_alert_result_within_deadline_not_alerted(factory):
    """截止未到 / 无 deadline 的 DELIVERED 不告警。"""
    await _seed(factory)

    async def deliver(task):
        return {"status": "ACCEPTED"}

    # 无 deadline（旧存量行为）：不注入 result_deadline
    await dispatch_delivery_once(factory, deliver=deliver, now=NOW)
    n = await alert_stuck_deliveries(
        factory,
        now=dt.datetime.now(dt.UTC) + dt.timedelta(days=365),
        pending_max_age_seconds=10.0**9,
        batch_limit=100,
    )
    assert n == 0


@pytest.mark.asyncio
async def test_alert_result_collected_not_alerted(factory):
    """已回收 result 的 DELIVERED 不告警（即使超截止）。"""
    await _seed(factory)

    async def deliver(task):
        return {"status": "ACCEPTED"}

    await dispatch_delivery_once(
        factory, deliver=deliver, now=NOW, result_deadline=lambda now: _DEADLINE
    )
    async with factory() as s:
        from sqlalchemy import update

        await s.execute(update(WedapImportDeliveryTask).values(result_collected_at=NOW))
        await s.commit()
    n = await alert_stuck_deliveries(
        factory,
        now=_DEADLINE + dt.timedelta(days=1),
        pending_max_age_seconds=10.0**9,
        batch_limit=100,
    )
    assert n == 0


@pytest.mark.asyncio
async def test_alert_result_overdue_without_accepted_at(factory):
    """存量行 accepted_at 为空（迁移前投递）也能告警，detail 记 accepted_at=None。"""
    await _seed(factory)
    async with factory() as s:
        from sqlalchemy import update

        await s.execute(
            update(WedapImportDeliveryTask).values(status="DELIVERED", result_deadline_at=_DEADLINE)
        )
        await s.commit()
    n = await alert_stuck_deliveries(
        factory,
        now=_DEADLINE + dt.timedelta(minutes=1),
        pending_max_age_seconds=10.0**9,
        batch_limit=100,
    )
    assert n == 1
    alerts = await _get_alerts(factory)
    assert alerts[0].kind == "RESULT_OVERDUE"
    assert "accepted_at=None" in alerts[0].detail


@pytest.mark.asyncio
async def test_alert_result_overdue_dedup(factory):
    """RESULT_OVERDUE 同批只告警一次（唯一约束去重）。"""
    await _seed(factory)

    async def deliver(task):
        return {"status": "ACCEPTED"}

    await dispatch_delivery_once(
        factory, deliver=deliver, now=NOW, result_deadline=lambda now: _DEADLINE
    )
    after = _DEADLINE + dt.timedelta(minutes=1)
    first = await alert_stuck_deliveries(
        factory, now=after, pending_max_age_seconds=10.0**9, batch_limit=100
    )
    again = await alert_stuck_deliveries(
        factory, now=after, pending_max_age_seconds=10.0**9, batch_limit=100
    )
    assert first == 1
    assert again == 0
    assert len(await _get_alerts(factory)) == 1


@pytest.mark.asyncio
async def test_dispatch_deadline_uses_post_deliver_clock(factory):
    """codex HIGH：deadline 按 deliver 完成后的真实受理时刻算，非本轮扫描起始。

    场景：扫描起始 01:59（anchor=02:00 前），notify 完成已 02:01（跨过 anchor）——
    deadline 必须基于 02:01 取次日 02:30，而非基于 01:59 取当日 02:30（scanner 已错过）。
    """
    await _seed(factory)
    scan_start = dt.datetime(2026, 6, 24, 1, 59, tzinfo=dt.UTC)
    after_anchor = dt.datetime(2026, 6, 24, 2, 1, tzinfo=dt.UTC)
    deadline_inputs = []

    async def deliver(task):
        return {"status": "ACCEPTED"}

    def deadline(accepted_at):
        deadline_inputs.append(accepted_at)
        return compute_result_deadline(accepted_at, anchor_hour=2, grace_minutes=30)

    await dispatch_delivery_once(
        factory,
        deliver=deliver,
        now=scan_start,
        result_deadline=deadline,
        clock=lambda: after_anchor,
    )
    assert deadline_inputs == [after_anchor]
    task = await _get_task(factory)
    assert task.accepted_at.replace(tzinfo=dt.UTC) == after_anchor
    assert task.notified_at.replace(tzinfo=dt.UTC) == after_anchor
    assert task.result_deadline_at.replace(tzinfo=dt.UTC) == dt.datetime(
        2026, 6, 25, 2, 30, tzinfo=dt.UTC
    )
