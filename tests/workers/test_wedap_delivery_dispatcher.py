import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.wedap_delivery import WedapImportDeliveryTask
from app.workers.wedap_delivery_dispatcher import make_deliver, make_on_terminal

_CONTENT = b'{"h":1}\n{"loanId":"L1"}\n'
_CHECKSUM = hashlib.sha256(_CONTENT).hexdigest()


def _task() -> WedapImportDeliveryTask:
    return WedapImportDeliveryTask(
        tenant_id="WBTHK01",
        request_id="wedap-import-B1",
        import_batch_no="BATCH-LEN-20260624-001",
        data_type="interest-accrual",
        import_date="20260624",
        staging_key="staging/k.jsonl",
        file_checksum=_CHECKSUM,
        file_size=len(_CONTENT),
        total_count=1,
    )


@pytest.mark.asyncio
async def test_make_deliver_binds_buckets_and_clients():
    s3 = MagicMock()
    s3.get_bytes = MagicMock(return_value=_CONTENT)
    s3.upload = MagicMock(return_value=_CHECKSUM)
    wedap = AsyncMock()
    wedap.notify_batch_uploaded = AsyncMock(return_value={"status": "ACCEPTED"})

    deliver = make_deliver(s3, wedap, staging_bucket="stg", wedap_bucket="wedap")
    await deliver(_task())

    # staging_bucket 绑定 → get_bytes 用 stg 桶 + 任务 staging_key
    s3.get_bytes.assert_called_once_with(bucket="stg", key="staging/k.jsonl")
    # wedap_bucket 绑定 → upload 用 wedap 桶
    assert s3.upload.call_args.kwargs["bucket"] == "wedap"
    wedap.notify_batch_uploaded.assert_awaited_once()


@pytest.mark.asyncio
async def test_make_on_terminal_posts_result():
    recon = AsyncMock()
    recon.post_result = AsyncMock()
    on_terminal = make_on_terminal(recon)
    await on_terminal(_task(), "DELIVERED", None)
    recon.post_result.assert_awaited_once_with(
        tenant_id="WBTHK01",
        import_batch_no="BATCH-LEN-20260624-001",
        status="DELIVERED",
        error=None,
    )


@pytest.mark.asyncio
async def test_make_on_terminal_swallows_callback_failure():
    recon = AsyncMock()
    recon.post_result = AsyncMock(side_effect=RuntimeError("recon down"))
    on_terminal = make_on_terminal(recon)
    # 回执失败不抛（best-effort）
    await on_terminal(_task(), "FAILED", "notify 5xx")
    recon.post_result.assert_awaited_once()
