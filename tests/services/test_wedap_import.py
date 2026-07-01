"""wedap flow-import 投递编排单测（mock S3 / wedap client）。"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.wedap_import import (
    UploadChecksumMismatch,
    build_s3_key,
    deliver_batch,
)

CHECKSUM = "a" * 64
CONTENT = b'{"channelId":"LEN"}\n{"loanId":"L1"}\n'


def test_build_s3_key():
    key = build_s3_key(
        data_type="interest-accrual",
        import_date="20260624",
        import_batch_no="BATCH-LEN-20260624-001",
    )
    assert key == "lending/import/interest-accrual/LEN/20260624/BATCH-LEN-20260624-001.jsonl"


def _s3(uploaded_checksum: str) -> MagicMock:
    s3 = MagicMock()
    s3.upload = MagicMock(return_value=uploaded_checksum)
    return s3


def _wedap(status: str = "ACCEPTED") -> AsyncMock:
    w = AsyncMock()
    w.notify_batch_uploaded = AsyncMock(return_value={"status": status, "processingId": "P1"})
    return w


async def _deliver(s3, wedap, **overrides):
    kwargs = dict(
        s3_client=s3,
        wedap_client=wedap,
        bucket="wedap-bucket",
        data_type="interest-accrual",
        import_date="20260624",
        import_batch_no="BATCH-LEN-20260624-001",
        content=CONTENT,
        checksum=CHECKSUM,
        file_size=len(CONTENT),
        total_count=1,
    )
    kwargs.update(overrides)
    return await deliver_batch(**kwargs)


@pytest.mark.asyncio
async def test_deliver_uploads_then_notifies():
    s3, wedap = _s3(CHECKSUM), _wedap("ACCEPTED")
    resp = await _deliver(s3, wedap)

    s3.upload.assert_called_once_with(
        bucket="wedap-bucket",
        key="lending/import/interest-accrual/LEN/20260624/BATCH-LEN-20260624-001.jsonl",
        content=CONTENT,
    )
    assert resp["status"] == "ACCEPTED"


@pytest.mark.asyncio
async def test_notify_payload_has_required_fields():
    s3, wedap = _s3(CHECKSUM), _wedap()
    await _deliver(s3, wedap)

    payload = wedap.notify_batch_uploaded.call_args.kwargs["payload"]
    assert payload["dataType"] == "interest-accrual"
    assert payload["channelId"] == "LEN"
    assert payload["importBatchNo"] == "BATCH-LEN-20260624-001"
    assert payload["importDate"] == "20260624"
    assert payload["fileChecksum"] == CHECKSUM
    assert payload["fileSize"] == len(CONTENT)
    assert payload["totalCount"] == 1
    assert payload["payloadSchemaVersion"] == "1.0"
    assert "replacesBatchNo" not in payload  # 非修复重传不带


@pytest.mark.asyncio
async def test_repair_resend_carries_replaces_batch_no():
    s3, wedap = _s3(CHECKSUM), _wedap()
    await _deliver(s3, wedap, replaces_batch_no="BATCH-LEN-20260624-000")

    payload = wedap.notify_batch_uploaded.call_args.kwargs["payload"]
    assert payload["replacesBatchNo"] == "BATCH-LEN-20260624-000"


@pytest.mark.asyncio
async def test_upload_checksum_mismatch_raises_and_skips_notify():
    s3, wedap = _s3("b" * 64), _wedap()  # S3 返回的 checksum 与生成侧不符
    with pytest.raises(UploadChecksumMismatch):
        await _deliver(s3, wedap)
    wedap.notify_batch_uploaded.assert_not_called()  # 上传损坏不通知


# --- presigned 分支（ADR-0001 P4）：request_presign → upload_via_presigned_put ---


def _presigned_s3(uploaded_checksum: str) -> MagicMock:
    s3 = MagicMock()
    s3.upload = MagicMock()  # 不应被调用（presigned 分支）
    s3.upload_via_presigned_put = MagicMock(return_value=uploaded_checksum)
    return s3


def _presigned_wedap(status: str = "ACCEPTED", url: str = "https://s3.ex/put?sig=a") -> AsyncMock:
    w = _wedap(status)
    w.request_presign = AsyncMock(return_value=url)
    return w


@pytest.mark.asyncio
async def test_deliver_presigned_requests_url_then_puts():
    s3, wedap = _presigned_s3(CHECKSUM), _presigned_wedap("ACCEPTED")
    resp = await _deliver(s3, wedap, presigned_enabled=True)

    wedap.request_presign.assert_awaited_once()
    kw = wedap.request_presign.await_args.kwargs
    assert kw["operation"] == "UPLOAD"
    assert kw["data_type"] == "interest-accrual"
    assert kw["channel_id"] == "LEN"
    assert kw["import_date"] == "20260624"
    assert kw["import_batch_no"] == "BATCH-LEN-20260624-001"
    # presigned PUT 到申请到的 url，boto3 upload 不被调用
    s3.upload_via_presigned_put.assert_called_once_with(
        url="https://s3.ex/put?sig=a", content=CONTENT
    )
    s3.upload.assert_not_called()
    assert resp["status"] == "ACCEPTED"


@pytest.mark.asyncio
async def test_deliver_presigned_checksum_mismatch_raises_and_skips_notify():
    s3, wedap = _presigned_s3("b" * 64), _presigned_wedap()  # 上传后 checksum 不符
    with pytest.raises(UploadChecksumMismatch):
        await _deliver(s3, wedap, presigned_enabled=True)
    wedap.notify_batch_uploaded.assert_not_called()


@pytest.mark.asyncio
async def test_deliver_boto3_branch_does_not_call_presign():
    """默认 presigned_enabled=False → 走 boto3 upload，不申请 presign（现网行为不变）。"""
    s3, wedap = _s3(CHECKSUM), _presigned_wedap()
    await _deliver(s3, wedap)  # 不传 presigned_enabled → 默认 False
    s3.upload.assert_called_once()
    wedap.request_presign.assert_not_awaited()
