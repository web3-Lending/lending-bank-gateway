"""wedap flow-import 投递编排：上传 S3 → 通知 wedap。

把生成侧（recon）产出的批次文件投递给 wedap：
  1. PUT 到约定 S3 key；校验上传内容 SHA-256 与生成侧一致（防上传损坏）。
  2. POST /bank/api/v1/import/batch-uploaded 通知（apikey 鉴权）。
返回 wedap 通知响应（status/processingId/resultFilePath/...），由调用方据 status 决定
轮询结果 / 修复重传。S3 上传为同步 boto3，经 asyncio.to_thread 不阻塞事件循环。
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.clients.s3 import S3FileClient
from app.clients.wedap import WedapClient

CHANNEL_ID = "LEN"
PAYLOAD_SCHEMA_VERSION = "1.0"


class UploadChecksumMismatch(Exception):
    """上传后 S3 内容 SHA-256 与生成侧 BatchFile.checksum 不一致（上传损坏）。"""


def build_s3_key(*, data_type: str, import_date: str, import_batch_no: str) -> str:
    """`wedap/import/{dataType}/LEN/{importDate}/{importBatchNo}.jsonl`（spec §4）。"""
    return f"wedap/import/{data_type}/{CHANNEL_ID}/{import_date}/{import_batch_no}.jsonl"


async def deliver_batch(
    *,
    s3_client: S3FileClient,
    wedap_client: WedapClient,
    bucket: str,
    data_type: str,
    import_date: str,
    import_batch_no: str,
    content: bytes,
    checksum: str,
    file_size: int,
    total_count: int,
    replaces_batch_no: str | None = None,
) -> dict[str, Any]:
    """上传 + 通知一次性投递，返回 wedap 通知响应。"""
    key = build_s3_key(
        data_type=data_type, import_date=import_date, import_batch_no=import_batch_no
    )
    uploaded = await asyncio.to_thread(s3_client.upload, bucket=bucket, key=key, content=content)
    if uploaded != checksum:
        raise UploadChecksumMismatch(f"{uploaded} != {checksum}")

    payload: dict[str, Any] = {
        "dataType": data_type,
        "channelId": CHANNEL_ID,
        "importBatchNo": import_batch_no,
        "importDate": import_date,
        "fileChecksum": checksum,
        "fileSize": file_size,
        "payloadSchemaVersion": PAYLOAD_SCHEMA_VERSION,
        "totalCount": total_count,
    }
    if replaces_batch_no is not None:
        payload["replacesBatchNo"] = replaces_batch_no

    return await wedap_client.notify_batch_uploaded(payload=payload)
