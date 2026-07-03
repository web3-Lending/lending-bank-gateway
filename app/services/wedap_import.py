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

# wedap 受理类 status（§6.1 护栏②）：KNOWN_BATCH_STATUS 7 值中真正代表"批次已被 wedap 接收、
# 后续会出 _result.json"的两值。其余 5 值（FILE_NOT_FOUND / CHECKSUM_MISMATCH / INVALID_PARAM /
# DUPLICATE_BATCH_CONFLICT / REPLACES_BATCH_NOT_FOUND）为业务拒绝——deliver_task 抛
# WedapBatchRejected 进 dispatch 退避重试（文件层问题重传可自愈；确定性错误重试到上限 FAILED），
# 不再被误记 DELIVERED（旧行为的 silent-failure：拒绝批永远等不到 result）。
ACCEPTED_BATCH_STATUS = frozenset({"ACCEPTED", "DUPLICATE_BATCH"})


class WedapBatchRejected(Exception):
    """wedap notify 返回业务拒绝 status（KNOWN_BATCH_STATUS 中非受理类的 5 值）。"""

    def __init__(self, status: str, message: str | None) -> None:
        super().__init__(f"wedap batch rejected: {status} ({message})")
        self.status = status


class UploadChecksumMismatch(Exception):
    """上传后 S3 内容 SHA-256 与生成侧 BatchFile.checksum 不一致（上传损坏）。"""


def build_s3_key(*, data_type: str, import_date: str, import_batch_no: str) -> str:
    """`lending/import/{dataType}/LEN/{importDate}/{importBatchNo}.jsonl`（spec §4）。"""
    return f"lending/import/{data_type}/{CHANNEL_ID}/{import_date}/{import_batch_no}.jsonl"


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
    presigned_enabled: bool = False,
) -> dict[str, Any]:
    """上传 + 通知一次性投递，返回 wedap 通知响应。

    上传路径二选一（ADR-0001 P4）：
      - presigned_enabled=False（默认）：boto3 直传 ``bucket`` + build_s3_key（需 wedap S3 凭证）。
      - presigned_enabled=True：向 web2-core 申请 presigned PUT URL 再 HTTP PUT（无需长期凭证）。
    两路径上传后都校验返回的内容 SHA-256 == 生成侧 checksum（防上传损坏），一致才 notify。
    """
    if presigned_enabled:
        url = await wedap_client.request_presign(
            operation="UPLOAD",
            data_type=data_type,
            channel_id=CHANNEL_ID,
            import_date=import_date,
            import_batch_no=import_batch_no,
        )
        uploaded = await asyncio.to_thread(
            s3_client.upload_via_presigned_put, url=url, content=content
        )
    else:
        key = build_s3_key(
            data_type=data_type, import_date=import_date, import_batch_no=import_batch_no
        )
        uploaded = await asyncio.to_thread(
            s3_client.upload, bucket=bucket, key=key, content=content
        )
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
