import datetime as dt
import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from app.core.db import build_engine, build_session_factory
from app.models.base import Base
from app.models.wedap_delivery import WedapImportDeliveryTask
from app.workers.wedap_delivery_dispatcher import make_deliver, make_on_terminal

_CONTENT = b'{"h":1}\n{"loanId":"L1"}\n'
_CHECKSUM = hashlib.sha256(_CONTENT).hexdigest()


@pytest.fixture()
async def factory(tmp_path):
    engine = build_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield build_session_factory(engine)
    await engine.dispose()


async def _insert(
    factory,
    *,
    status="DELIVERED",
    callback_sent_at=None,
    callback_locked_at=None,
    batch="BATCH-LEN-20260624-001",
):
    async with factory() as s:
        s.add(
            WedapImportDeliveryTask(
                tenant_id="WBTHK01",
                request_id=f"wedap-import-{batch}",
                import_batch_no=batch,
                data_type="interest-accrual",
                import_date="20260624",
                staging_key="staging/k.jsonl",
                file_checksum=_CHECKSUM,
                file_size=len(_CONTENT),
                total_count=1,
                status=status,
                last_error=None,
                callback_sent_at=callback_sent_at,
                callback_locked_at=callback_locked_at,
            )
        )
        await s.commit()


async def _get(factory, batch="BATCH-LEN-20260624-001"):
    async with factory() as s:
        return (
            await s.execute(
                select(WedapImportDeliveryTask).where(
                    WedapImportDeliveryTask.import_batch_no == batch
                )
            )
        ).scalar_one()


@pytest.mark.asyncio
async def test_make_deliver_binds_buckets_and_clients(factory):
    s3 = MagicMock()
    s3.get_bytes = MagicMock(return_value=_CONTENT)
    s3.upload = MagicMock(return_value=_CHECKSUM)
    wedap = AsyncMock()
    wedap.notify_batch_uploaded = AsyncMock(return_value={"status": "ACCEPTED"})

    deliver = make_deliver(s3, wedap, staging_bucket="stg", wedap_bucket="wedap")
    await _insert(factory, status="SENDING")
    await deliver(await _get(factory))

    s3.get_bytes.assert_called_once_with(bucket="stg", key="staging/k.jsonl")
    assert s3.upload.call_args.kwargs["bucket"] == "wedap"
    wedap.notify_batch_uploaded.assert_awaited_once()


@pytest.mark.asyncio
async def test_make_on_terminal_success_marks_callback_sent(factory):
    await _insert(factory, status="DELIVERED", callback_sent_at=None)
    task = await _get(factory)
    recon = AsyncMock()
    recon.post_result = AsyncMock()

    on_terminal = make_on_terminal(recon, factory)
    await on_terminal(task, "DELIVERED", None)

    recon.post_result.assert_awaited_once()
    assert (await _get(factory)).callback_sent_at is not None  # 成功→标记送达


@pytest.mark.asyncio
async def test_make_on_terminal_failure_leaves_callback_unsent(factory):
    await _insert(factory, status="FAILED", callback_sent_at=None)
    task = await _get(factory)
    recon = AsyncMock()
    recon.post_result = AsyncMock(side_effect=RuntimeError("recon down"))

    on_terminal = make_on_terminal(recon, factory)
    await on_terminal(task, "FAILED", "notify 5xx")  # 不抛

    recon.post_result.assert_awaited_once()
    assert (await _get(factory)).callback_sent_at is None  # 失败→留空待重发


@pytest.mark.asyncio
async def test_resend_via_on_terminal_picks_unsent_terminal(factory):
    """resend pass(由 run_forever 调)用 on_terminal 重发终态未送达回执。"""
    from app.services.wedap_delivery import resend_pending_callbacks_once

    await _insert(factory, status="DELIVERED", callback_sent_at=None, batch="B-UNSENT")
    await _insert(
        factory,
        status="DELIVERED",
        callback_sent_at=dt.datetime(2026, 6, 24, tzinfo=dt.UTC),
        batch="B-SENT",
    )
    await _insert(factory, status="PENDING", callback_sent_at=None, batch="B-PENDING")
    recon = AsyncMock()
    recon.post_result = AsyncMock()
    on_terminal = make_on_terminal(recon, factory)

    n = await resend_pending_callbacks_once(factory, send=on_terminal, now=dt.datetime.now(dt.UTC))

    assert n == 1  # 只 B-UNSENT(终态+未送达);B-SENT 已送达跳过,B-PENDING 非终态跳过
    recon.post_result.assert_awaited_once()
    assert (await _get(factory, "B-UNSENT")).callback_sent_at is not None


@pytest.mark.asyncio
async def test_resend_claim_skips_freshly_locked(factory):
    """已被别的实例 claim(callback_locked_at 新鲜)→ resend 跳过,不重发。"""
    from app.services.wedap_delivery import resend_pending_callbacks_once

    now = dt.datetime(2026, 6, 24, 12, 0, tzinfo=dt.UTC)
    await _insert(
        factory,
        status="DELIVERED",
        callback_sent_at=None,
        callback_locked_at=now - dt.timedelta(seconds=10),
        batch="B-LOCKED",
    )
    recon = AsyncMock()
    recon.post_result = AsyncMock()
    n = await resend_pending_callbacks_once(factory, send=make_on_terminal(recon, factory), now=now)
    assert n == 0
    recon.post_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_resend_claim_reclaims_stale_lock(factory):
    """残留锁(超时)→ 可重新 claim 重发。"""
    from app.services.wedap_delivery import resend_pending_callbacks_once

    now = dt.datetime(2026, 6, 24, 12, 0, tzinfo=dt.UTC)
    await _insert(
        factory,
        status="DELIVERED",
        callback_sent_at=None,
        callback_locked_at=now - dt.timedelta(seconds=3600),
        batch="B-STALE",
    )
    recon = AsyncMock()
    recon.post_result = AsyncMock()
    n = await resend_pending_callbacks_once(factory, send=make_on_terminal(recon, factory), now=now)
    assert n == 1
    recon.post_result.assert_awaited_once()


@pytest.mark.asyncio
async def test_resend_skips_when_claim_lost(factory, monkeypatch):
    """scan 到但 claim 竞败(别的实例抢先)→ continue 跳过不重发。"""
    from app.services import wedap_delivery as _wd
    from app.services.wedap_delivery import resend_pending_callbacks_once

    await _insert(factory, status="DELIVERED", callback_sent_at=None, batch="B-RACE")
    monkeypatch.setattr(_wd, "_claim_callback", AsyncMock(return_value=False))
    recon = AsyncMock()
    recon.post_result = AsyncMock()
    n = await resend_pending_callbacks_once(
        factory, send=make_on_terminal(recon, factory), now=dt.datetime(2026, 6, 24, tzinfo=dt.UTC)
    )
    assert n == 0
    recon.post_result.assert_not_awaited()
