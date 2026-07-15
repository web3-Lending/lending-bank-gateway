"""outbox 服务单元测试：投递、退避、DEAD、重放、dedup_key 幂等。"""

import httpx
import pytest
import respx

from app.core.db import build_engine, build_session_factory
from app.models.base import Base
from app.models.callback import CallbackOutbox
from app.services.outbox import (
    _finalize_after_send,
    _reclaim_stale_sending,
    dispatch_once,
    enqueue_forward,
    replay_dead,
)


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
    """转发请求：无 dedup_key 时 X-Request-Id = outbox-{id}（向后兼容）。"""
    route = respx.post("http://lifecycle/cb").mock(return_value=httpx.Response(200))
    oid = await _enqueue(factory)
    await dispatch_once(factory, targets=TARGETS, max_attempts=3)
    assert route.called
    req = route.calls.last.request
    assert req.headers.get("X-Caller-Service") == "internal"
    assert req.headers.get("X-Tenant-Id") == "OCBC"
    assert req.headers.get("X-Trace-Id") == f"outbox-{oid}"
    assert req.headers.get("X-Request-Id") == f"outbox-{oid}"


# ---------------------------------------------------------------------------
# P1：dedup_key 幂等测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dedup_key_same_enqueue_returns_existing_row(factory) -> None:
    """同 dedup_key 重复 enqueue 只产生一行，第二次返回既有行。"""
    async with factory() as s:
        async with s.begin():
            row1 = await enqueue_forward(
                s,
                tenant_id="OCBC",
                target="lifecycle",
                payload={"bizSeqNo": "B1"},
                dedup_key="fwd-req-001",
            )
            id1 = row1.id

    async with factory() as s:
        async with s.begin():
            row2 = await enqueue_forward(
                s,
                tenant_id="OCBC",
                target="lifecycle",
                payload={"bizSeqNo": "B1"},
                dedup_key="fwd-req-001",
            )
            id2 = row2.id

    # 同一行（id 相同），数据库只有 1 条
    assert id1 == id2
    from sqlalchemy import select

    async with factory() as s:
        rows = (await s.execute(select(CallbackOutbox))).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_different_dedup_keys_produce_separate_rows(factory) -> None:
    """不同 dedup_key 各产生独立行。"""
    async with factory() as s:
        async with s.begin():
            r1 = await enqueue_forward(
                s,
                tenant_id="OCBC",
                target="lifecycle",
                payload={"bizSeqNo": "B1"},
                dedup_key="fwd-req-001",
            )
            r2 = await enqueue_forward(
                s,
                tenant_id="OCBC",
                target="lifecycle",
                payload={"bizSeqNo": "B2"},
                dedup_key="fwd-req-002",
            )
    assert r1.id != r2.id
    from sqlalchemy import select

    async with factory() as s:
        rows = (await s.execute(select(CallbackOutbox))).scalars().all()
    assert len(rows) == 2


@pytest.mark.asyncio
@respx.mock
async def test_dispatch_uses_dedup_key_as_request_id(factory) -> None:
    """有 dedup_key 时 X-Request-Id 使用 dedup_key，跨重放保持稳定（下游幂等键）。"""
    route = respx.post("http://lifecycle/cb").mock(return_value=httpx.Response(200))
    async with factory() as s:
        async with s.begin():
            await enqueue_forward(
                s,
                tenant_id="OCBC",
                target="lifecycle",
                payload={"bizSeqNo": "B1"},
                dedup_key="fwd-cb-req-abc",
            )
    await dispatch_once(factory, targets=TARGETS, max_attempts=3)
    assert route.called
    req = route.calls.last.request
    # dedup_key 作为 X-Request-Id，下游重放时幂等键稳定
    assert req.headers.get("X-Request-Id") == "fwd-cb-req-abc"


@pytest.mark.asyncio
async def test_none_dedup_key_no_dedup_check(factory) -> None:
    """dedup_key=None 时每次 enqueue 产生新行（无幂等保证，向后兼容）。"""
    async with factory() as s:
        async with s.begin():
            r1 = await enqueue_forward(
                s,
                tenant_id="OCBC",
                target="lifecycle",
                payload={"bizSeqNo": "B1"},
                dedup_key=None,
            )
            r2 = await enqueue_forward(
                s,
                tenant_id="OCBC",
                target="lifecycle",
                payload={"bizSeqNo": "B1"},
                dedup_key=None,
            )
    assert r1.id != r2.id


# ---------------------------------------------------------------------------
# A-M-001 / A-m-001：原子 claim 回收 + trace_id 透传
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_dispatch_forwards_original_trace_id(factory) -> None:
    """A-m-001：enqueue 带 trace_id → dispatch 转发 X-Trace-Id=原始 trace_id（非 outbox-{id}）。"""
    route = respx.post("http://lifecycle/cb").mock(return_value=httpx.Response(200))
    async with factory() as s:
        async with s.begin():
            await enqueue_forward(
                s,
                tenant_id="OCBC",
                target="lifecycle",
                payload={"bizSeqNo": "B1"},
                dedup_key="fwd-trace-1",
                trace_id="trc-original-xyz",
            )
    await dispatch_once(factory, targets=TARGETS, max_attempts=3)
    assert route.called
    assert route.calls.last.request.headers.get("X-Trace-Id") == "trc-original-xyz"


@pytest.mark.asyncio
@respx.mock
async def test_dispatch_reclaims_stale_sending_then_resends(factory) -> None:
    """A-M-001：卡死 SENDING 超时行被 reclaim 回 FAILED 后重投成功 → SENT，locked_at 清空。"""
    import datetime as dt

    route = respx.post("http://lifecycle/cb").mock(return_value=httpx.Response(200))
    async with factory() as s:
        async with s.begin():
            s.add(
                CallbackOutbox(
                    tenant_id="OCBC",
                    target="lifecycle",
                    payload={"bizSeqNo": "B1"},
                    status="SENDING",
                    locked_at=dt.datetime(2020, 1, 1, tzinfo=dt.UTC),
                    dedup_key="fwd-stale-1",
                )
            )
    await dispatch_once(factory, targets=TARGETS, max_attempts=3, claim_timeout_seconds=1.0)
    assert route.called
    from sqlalchemy import select

    async with factory() as s:
        rows = (await s.execute(select(CallbackOutbox))).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "SENT"
    assert rows[0].locked_at is None


@pytest.mark.asyncio
@respx.mock
async def test_dispatch_skips_fresh_sending_not_reclaimed(factory) -> None:
    """A-M-001：新鲜 SENDING（locked_at 近）不被 reclaim、也不被 claim（视为别副本在投）。"""
    import datetime as dt

    respx.post("http://lifecycle/cb").mock(return_value=httpx.Response(200))
    async with factory() as s:
        async with s.begin():
            s.add(
                CallbackOutbox(
                    tenant_id="OCBC",
                    target="lifecycle",
                    payload={"bizSeqNo": "B1"},
                    status="SENDING",
                    locked_at=dt.datetime.now(dt.UTC),
                    dedup_key="fwd-fresh-1",
                )
            )
    handled = await dispatch_once(
        factory, targets=TARGETS, max_attempts=3, claim_timeout_seconds=300.0
    )
    assert handled == 0
    from sqlalchemy import select

    async with factory() as s:
        row = (await s.execute(select(CallbackOutbox))).scalars().one()
    assert row.status == "SENDING"  # 仍由（假想的）别副本持有，未被本轮动


# ---------------------------------------------------------------------------
# A-M-002（migration 0009）：claim_token CAS 防迟到副本覆盖终态
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_dispatch_success_clears_claim_token(factory) -> None:
    """成功投递后 claim_token 被清空（终态行无活跃 claim）。"""
    respx.post("http://lifecycle/cb").mock(return_value=httpx.Response(200))
    oid = await _enqueue(factory)
    await dispatch_once(factory, targets=TARGETS, max_attempts=3)
    async with factory() as s:
        row = await s.get(CallbackOutbox, oid)
    assert row.status == "SENT"
    assert row.claim_token is None


@pytest.mark.asyncio
async def test_finalize_cas_rejects_stale_claim_token(factory) -> None:
    """CAS：claim_token 不匹配（迟到副本 A）→ 终结写回被拒，B 的行状态/令牌/attempts 不变。"""
    import datetime as dt

    # 行已被「副本 B」claim 并持 token=tok-B
    async with factory() as s:
        async with s.begin():
            row = CallbackOutbox(
                tenant_id="OCBC",
                target="lifecycle",
                payload={"bizSeqNo": "B1"},
                status="SENDING",
                locked_at=dt.datetime.now(dt.UTC),
                claim_token="tok-B",  # noqa: S106
            )
            s.add(row)
            await s.flush()
            oid = row.id

    # 「副本 A」（持旧 token=tok-A）迟到终结 → CAS 未命中
    ok = await _finalize_after_send(
        factory,
        oid=oid,
        claim_token="tok-A",  # noqa: S106
        attempts_before=0,
        ok_flag=True,
        error=None,
        max_attempts=3,
        backoff_base_seconds=0,
    )
    assert ok is False
    async with factory() as s:
        row = await s.get(CallbackOutbox, oid)
    assert row.status == "SENDING"  # B 仍持有，未被 A 覆盖
    assert row.claim_token == "tok-B"  # noqa: S105
    assert row.attempts == 0


@pytest.mark.asyncio
async def test_finalize_cas_succeeds_with_matching_token(factory) -> None:
    """CAS：claim_token 匹配 → 终结成功，状态 SENT、令牌清空、attempts+1。"""
    import datetime as dt

    async with factory() as s:
        async with s.begin():
            row = CallbackOutbox(
                tenant_id="OCBC",
                target="lifecycle",
                payload={"bizSeqNo": "B1"},
                status="SENDING",
                locked_at=dt.datetime.now(dt.UTC),
                claim_token="tok-A",  # noqa: S106
                attempts=0,
            )
            s.add(row)
            await s.flush()
            oid = row.id

    ok = await _finalize_after_send(
        factory,
        oid=oid,
        claim_token="tok-A",  # noqa: S106
        attempts_before=0,
        ok_flag=True,
        error=None,
        max_attempts=3,
        backoff_base_seconds=0,
    )
    assert ok is True
    async with factory() as s:
        row = await s.get(CallbackOutbox, oid)
    assert row.status == "SENT"
    assert row.claim_token is None
    assert row.attempts == 1


@pytest.mark.asyncio
async def test_finalize_cas_failed_branch_sets_retry(factory) -> None:
    """CAS 命中 + ok_flag=False + 未达 max → FAILED + next_retry_at 设定 + 令牌清空。"""
    import datetime as dt

    async with factory() as s:
        async with s.begin():
            row = CallbackOutbox(
                tenant_id="OCBC",
                target="lifecycle",
                payload={"bizSeqNo": "B1"},
                status="SENDING",
                locked_at=dt.datetime.now(dt.UTC),
                claim_token="tok-A",  # noqa: S106
                attempts=0,
            )
            s.add(row)
            await s.flush()
            oid = row.id

    ok = await _finalize_after_send(
        factory,
        oid=oid,
        claim_token="tok-A",  # noqa: S106
        attempts_before=0,
        ok_flag=False,
        error="http 502",
        max_attempts=3,
        backoff_base_seconds=1,
    )
    assert ok is True
    async with factory() as s:
        row = await s.get(CallbackOutbox, oid)
    assert row.status == "FAILED"
    assert row.last_error == "http 502"
    assert row.next_retry_at is not None
    assert row.claim_token is None
    assert row.attempts == 1


@pytest.mark.asyncio
async def test_reclaim_clears_claim_token(factory) -> None:
    """reclaim 把超时 SENDING 回 FAILED 时清空 claim_token（旧 claim 作废）。"""
    import datetime as dt

    async with factory() as s:
        async with s.begin():
            row = CallbackOutbox(
                tenant_id="OCBC",
                target="lifecycle",
                payload={"bizSeqNo": "B1"},
                status="SENDING",
                locked_at=dt.datetime(2020, 1, 1, tzinfo=dt.UTC),
                claim_token="tok-stale",  # noqa: S106
            )
            s.add(row)
            await s.flush()
            oid = row.id

    await _reclaim_stale_sending(factory, now=dt.datetime.now(dt.UTC), claim_timeout_seconds=1.0)
    async with factory() as s:
        row = await s.get(CallbackOutbox, oid)
    assert row.status == "FAILED"
    assert row.claim_token is None
