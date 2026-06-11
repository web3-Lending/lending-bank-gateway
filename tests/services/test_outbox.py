"""outbox 服务单元测试：投递、退避、DEAD、重放。"""

import httpx
import pytest
import respx

from app.core.db import build_engine, build_session_factory
from app.models.base import Base
from app.models.callback import CallbackOutbox
from app.services.outbox import dispatch_once, enqueue_forward, replay_dead


@pytest.fixture()
async def factory():
    engine = build_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield build_session_factory(engine)
    await engine.dispose()


async def _enqueue(factory) -> int:
    async with factory() as s:
        async with s.begin():
            row = await enqueue_forward(
                s, tenant_id="OCBC", target="lifecycle", payload={"bizSeqNo": "B1"}
            )
            return row.id


TARGETS = {"lifecycle": "http://lifecycle/cb"}


@pytest.mark.asyncio
@respx.mock
async def test_dispatch_success_marks_sent(factory) -> None:
    respx.post("http://lifecycle/cb").mock(return_value=httpx.Response(200))
    oid = await _enqueue(factory)
    await dispatch_once(factory, targets=TARGETS, max_attempts=3)
    async with factory() as s:
        assert (await s.get(CallbackOutbox, oid)).status == "SENT"


@pytest.mark.asyncio
@respx.mock
async def test_dispatch_failure_retries_then_dead(factory) -> None:
    respx.post("http://lifecycle/cb").mock(return_value=httpx.Response(502))
    oid = await _enqueue(factory)
    for _ in range(3):
        await dispatch_once(factory, targets=TARGETS, max_attempts=3, backoff_base_seconds=0)
    async with factory() as s:
        row = await s.get(CallbackOutbox, oid)
        assert row.status == "DEAD" and row.attempts == 3


@pytest.mark.asyncio
@respx.mock
async def test_replay_dead_resets_to_pending(factory) -> None:
    respx.post("http://lifecycle/cb").mock(return_value=httpx.Response(502))
    oid = await _enqueue(factory)
    for _ in range(3):
        await dispatch_once(factory, targets=TARGETS, max_attempts=3, backoff_base_seconds=0)
    async with factory() as s:
        async with s.begin():
            assert await replay_dead(s, outbox_id=oid) is True
    async with factory() as s:
        assert (await s.get(CallbackOutbox, oid)).status == "PENDING"


@pytest.mark.asyncio
@respx.mock
async def test_dispatch_skips_not_yet_due(factory) -> None:
    """next_retry_at 未到期的 FAILED 行不应被投递。"""

    respx.post("http://lifecycle/cb").mock(return_value=httpx.Response(502))
    oid = await _enqueue(factory)
    # 第一次投递失败，设置 next_retry_at 为未来
    await dispatch_once(factory, targets=TARGETS, max_attempts=3, backoff_base_seconds=3600)
    # 第二次投递：next_retry_at 未到期，应跳过
    handled = await dispatch_once(
        factory, targets=TARGETS, max_attempts=3, backoff_base_seconds=3600
    )
    assert handled == 0
    async with factory() as s:
        row = await s.get(CallbackOutbox, oid)
        # 只触发过 1 次实际投递（第一次），attempts=1
        assert row.attempts == 1
        assert row.status == "FAILED"


@pytest.mark.asyncio
@respx.mock
async def test_dispatch_unknown_target_marks_failed(factory) -> None:
    """未知 target → status=FAILED，last_error 含提示。"""
    # 用不在 TARGETS 里的 target 创建 outbox 行
    async with factory() as s:
        async with s.begin():
            row = await enqueue_forward(
                s, tenant_id="OCBC", target="unknown_svc", payload={"bizSeqNo": "B2"}
            )
            oid = row.id
    await dispatch_once(factory, targets=TARGETS, max_attempts=3)
    async with factory() as s:
        row = await s.get(CallbackOutbox, oid)
        assert row.status == "FAILED"
        assert row.last_error is not None
        assert "unknown_svc" in row.last_error


@pytest.mark.asyncio
@respx.mock
async def test_dispatch_connect_timeout_marks_failed(factory) -> None:
    """网络异常（ConnectTimeout）→ status=FAILED，last_error 含异常类名。"""
    respx.post("http://lifecycle/cb").mock(side_effect=httpx.ConnectTimeout("timeout"))
    oid = await _enqueue(factory)
    await dispatch_once(factory, targets=TARGETS, max_attempts=3, backoff_base_seconds=0)
    async with factory() as s:
        row = await s.get(CallbackOutbox, oid)
        assert row.status == "FAILED"
        assert row.last_error is not None
        assert "ConnectTimeout" in row.last_error


@pytest.mark.asyncio
async def test_replay_non_dead_returns_false(factory) -> None:
    """非 DEAD 行调用 replay_dead → 返回 False，status 不变。"""
    oid = await _enqueue(factory)
    async with factory() as s:
        async with s.begin():
            result = await replay_dead(s, outbox_id=oid)
    assert result is False
    async with factory() as s:
        row = await s.get(CallbackOutbox, oid)
        assert row.status == "PENDING"


@pytest.mark.asyncio
@respx.mock
async def test_dispatch_sends_correct_headers(factory) -> None:
    """转发请求必须携带 X-Caller-Service、X-Tenant-Id 头（S2S 规范）。"""
    route = respx.post("http://lifecycle/cb").mock(return_value=httpx.Response(200))
    await _enqueue(factory)
    await dispatch_once(factory, targets=TARGETS, max_attempts=3)
    assert route.called
    req = route.calls.last.request
    assert req.headers.get("X-Caller-Service") == "lending-bank-gateway"
    assert req.headers.get("X-Tenant-Id") == "OCBC"
