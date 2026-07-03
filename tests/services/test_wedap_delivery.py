import datetime as dt
import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.db import build_engine, build_session_factory
from app.models.base import Base
from app.models.wedap_delivery import WedapImportDeliveryTask
from app.services.wedap_delivery import (
    StagingChecksumMismatch,
    compute_next_retry,
    deliver_task,
    enqueue_delivery,
)


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


_CONTENT = b'{"h":1}\n{"loanId":"L1"}\n'
_CHECKSUM = hashlib.sha256(_CONTENT).hexdigest()


def _task(checksum: str) -> WedapImportDeliveryTask:
    return WedapImportDeliveryTask(
        tenant_id="WBTHK01",
        request_id="wedap-import-B1",
        import_batch_no="BATCH-LEN-20260624-001",
        data_type="interest-accrual",
        import_date="20260624",
        staging_key="staging/k.jsonl",
        file_checksum=checksum,
        file_size=len(_CONTENT),
        total_count=1,
    )


@pytest.mark.asyncio
async def test_deliver_task_reads_staging_and_delivers():
    s3 = MagicMock()
    s3.get_bytes = MagicMock(return_value=_CONTENT)
    s3.upload = MagicMock(return_value=_CHECKSUM)  # 上传后 checksum 与生成侧一致
    wedap = AsyncMock()
    wedap.notify_batch_uploaded = AsyncMock(return_value={"status": "ACCEPTED"})

    await deliver_task(
        _task(_CHECKSUM),
        s3_client=s3,
        wedap_client=wedap,
        staging_bucket="stg",
        wedap_bucket="wedap",
    )

    s3.get_bytes.assert_called_once_with(bucket="stg", key="staging/k.jsonl")
    s3.upload.assert_called_once()
    wedap.notify_batch_uploaded.assert_awaited_once()
    payload = wedap.notify_batch_uploaded.await_args.kwargs["payload"]
    assert payload["importBatchNo"] == "BATCH-LEN-20260624-001"
    assert payload["fileChecksum"] == _CHECKSUM


@pytest.mark.asyncio
async def test_deliver_task_staging_checksum_mismatch_aborts():
    s3 = MagicMock()
    s3.get_bytes = MagicMock(return_value=_CONTENT)  # 实际字节
    s3.upload = MagicMock()
    wedap = AsyncMock()

    with pytest.raises(StagingChecksumMismatch):
        await deliver_task(
            _task("b" * 64),
            s3_client=s3,
            wedap_client=wedap,  # 记录 checksum 与实际不符
            staging_bucket="stg",
            wedap_bucket="wedap",
        )
    s3.upload.assert_not_called()  # 校验失败不上传脏字节
    wedap.notify_batch_uploaded.assert_not_awaited()


@pytest.mark.asyncio
async def test_enqueue_and_serialize_returns_response(tmp_path):
    from app.services.wedap_delivery import enqueue_and_serialize

    engine = build_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    resp = await enqueue_and_serialize(build_session_factory(engine), **_KW)
    await engine.dispose()

    assert resp["importBatchNo"] == "BATCH-LEN-20260624-001"
    assert resp["requestId"] == "wedap-import-BATCH-LEN-20260624-001"
    assert resp["status"] == "PENDING"
    assert resp["taskId"] is not None


# ─────────────────────── §6.1 护栏②：notify 受理判定 ───────────────────────

from app.services.wedap_import import WedapBatchRejected  # noqa: E402


@pytest.mark.asyncio
async def test_deliver_task_returns_notify_response():
    """受理（ACCEPTED）→ 返回响应体（dispatch 落 result_file_path 用）。"""
    s3 = MagicMock()
    s3.get_bytes = MagicMock(return_value=_CONTENT)
    s3.upload = MagicMock(return_value=_CHECKSUM)
    wedap = AsyncMock()
    wedap.notify_batch_uploaded = AsyncMock(
        return_value={"status": "ACCEPTED", "resultFilePath": "p/_result.json"}
    )

    resp = await deliver_task(
        _task(_CHECKSUM),
        s3_client=s3,
        wedap_client=wedap,
        staging_bucket="stg",
        wedap_bucket="wedap",
    )
    assert resp == {"status": "ACCEPTED", "resultFilePath": "p/_result.json"}


@pytest.mark.asyncio
async def test_deliver_task_duplicate_batch_is_accepted():
    """DUPLICATE_BATCH（同号同 checksum 重投）属受理类，不抛。"""
    s3 = MagicMock()
    s3.get_bytes = MagicMock(return_value=_CONTENT)
    s3.upload = MagicMock(return_value=_CHECKSUM)
    wedap = AsyncMock()
    wedap.notify_batch_uploaded = AsyncMock(return_value={"status": "DUPLICATE_BATCH"})

    resp = await deliver_task(
        _task(_CHECKSUM),
        s3_client=s3,
        wedap_client=wedap,
        staging_bucket="stg",
        wedap_bucket="wedap",
    )
    assert resp["status"] == "DUPLICATE_BATCH"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        "FILE_NOT_FOUND",
        "CHECKSUM_MISMATCH",
        "INVALID_PARAM",
        "DUPLICATE_BATCH_CONFLICT",
        "REPLACES_BATCH_NOT_FOUND",
    ],
)
async def test_deliver_task_rejected_status_raises(status):
    """非受理 status → WedapBatchRejected（进 dispatch 退避重试，不误记 DELIVERED）。"""
    s3 = MagicMock()
    s3.get_bytes = MagicMock(return_value=_CONTENT)
    s3.upload = MagicMock(return_value=_CHECKSUM)
    wedap = AsyncMock()
    wedap.notify_batch_uploaded = AsyncMock(
        return_value={"status": status, "message": "boom"}
    )

    with pytest.raises(WedapBatchRejected) as exc:
        await deliver_task(
            _task(_CHECKSUM),
            s3_client=s3,
            wedap_client=wedap,
            staging_bucket="stg",
            wedap_bucket="wedap",
        )
    assert exc.value.status == status
    assert status in str(exc.value)
