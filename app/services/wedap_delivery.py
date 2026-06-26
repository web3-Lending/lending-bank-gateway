"""wedap flow-import 投递服务（§4.3「A+异步回执」· gateway 侧）。

- enqueue_delivery：recon enqueue 交单 → 插一条 PENDING 投递任务（幂等）。
- dispatch_delivery_once：dispatcher 扫 PENDING/可重试任务 → 取 staging 字节 → deliver_batch
  （S3 upload + checksum verify + wedap notify）→ DELIVERED/FAILED + 退避重试。

权威业务状态在 recon 的 wedap_export_batch；本表只是执行账本。投递终态由调用方经回执回写 recon。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.s3 import S3FileClient
from app.clients.wedap import WedapClient
from app.models.wedap_delivery import WedapImportDeliveryTask
from app.services.wedap_import import deliver_batch

logger = logging.getLogger(__name__)


async def enqueue_and_serialize(
    factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: str,
    request_id: str,
    import_batch_no: str,
    data_type: str,
    import_date: str,
    staging_key: str,
    file_checksum: str,
    file_size: int,
    total_count: int,
) -> dict[str, Any]:
    """开本地事务 enqueue 一条投递任务并序列化为响应 dict（端点编排下沉，便于直测）。"""
    async with factory() as session:
        async with session.begin():
            task = await enqueue_delivery(
                session,
                tenant_id=tenant_id,
                request_id=request_id,
                import_batch_no=import_batch_no,
                data_type=data_type,
                import_date=import_date,
                staging_key=staging_key,
                file_checksum=file_checksum,
                file_size=file_size,
                total_count=total_count,
            )
        return {
            "importBatchNo": task.import_batch_no,
            "requestId": task.request_id,
            "status": task.status,
            "taskId": task.id,
        }


async def enqueue_delivery(
    session: AsyncSession,
    *,
    tenant_id: str,
    request_id: str,
    import_batch_no: str,
    data_type: str,
    import_date: str,
    staging_key: str,
    file_checksum: str,
    file_size: int,
    total_count: int,
) -> WedapImportDeliveryTask:
    """插一条 PENDING 投递任务并返回；幂等。

    幂等键 (tenant_id, import_batch_no)：recon 重发同号 enqueue → 返回既有任务不重插
    （gateway 重试复用同号同 checksum，不生成新号，方案 §4.4）。并发竞态由唯一约束兜底。
    """
    existing = (
        await session.execute(
            select(WedapImportDeliveryTask).where(
                WedapImportDeliveryTask.tenant_id == tenant_id,
                WedapImportDeliveryTask.import_batch_no == import_batch_no,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    try:
        async with session.begin_nested():
            task = WedapImportDeliveryTask(
                tenant_id=tenant_id,
                request_id=request_id,
                import_batch_no=import_batch_no,
                data_type=data_type,
                import_date=import_date,
                staging_key=staging_key,
                file_checksum=file_checksum,
                file_size=file_size,
                total_count=total_count,
                status="PENDING",
            )
            session.add(task)
            await session.flush()
        return task
    except IntegrityError:  # pragma: no cover - 并发竞态：单线程测试不可达
        return (
            await session.execute(
                select(WedapImportDeliveryTask).where(
                    WedapImportDeliveryTask.tenant_id == tenant_id,
                    WedapImportDeliveryTask.import_batch_no == import_batch_no,
                )
            )
        ).scalar_one()


def compute_next_retry(
    now: dt.datetime, attempts: int, *, base_seconds: float = 60.0
) -> dt.datetime:
    """退避：next_retry_at = now + base * 2^(attempts-1)（指数退避，与 outbox 一致）。"""
    delay = base_seconds * (2 ** max(0, attempts - 1))
    return now + dt.timedelta(seconds=delay)


async def dispatch_delivery_once(
    factory: async_sessionmaker[AsyncSession],
    *,
    deliver: Callable[[WedapImportDeliveryTask], Awaitable[None]],
    now: dt.datetime,
    max_attempts: int = 5,
    base_seconds: float = 60.0,
    on_terminal: Callable[[WedapImportDeliveryTask, str, str | None], Awaitable[None]]
    | None = None,
) -> int:
    """扫一轮可投递任务并逐条投递，返回处理条数。

    可投递 = status=PENDING 且 (next_retry_at 为空 或 已到点)。投递动作 deliver 由调用方注入
    （取 staging 字节 + deliver_batch），本函数只管状态机：成功→DELIVERED；失败→attempts+1，
    未达上限回 PENDING + 退避，达上限→FAILED。外呼在事务外，更新各自独立短事务（与 outbox 一致）。
    on_terminal（可选）在任务进终态（DELIVERED/FAILED）且已 commit 后触发，用于回执 recon。
    """
    async with factory() as snap_session:
        tasks = list(
            (
                await snap_session.execute(
                    select(WedapImportDeliveryTask).where(
                        WedapImportDeliveryTask.status == "PENDING",
                        or_(
                            WedapImportDeliveryTask.next_retry_at.is_(None),
                            WedapImportDeliveryTask.next_retry_at <= now,
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )

    processed = 0
    for task in tasks:
        error: str | None = None
        try:
            await deliver(task)
        except Exception as exc:  # noqa: BLE001 - 投递失败统一进退避，错误存 last_error
            error = str(exc)
            logger.warning(
                "wedap_delivery: 投递失败 batch=%s attempt=%s err=%s",
                task.import_batch_no,
                task.attempts + 1,
                error,
            )

        terminal: str | None = None
        async with factory() as upd_session:
            row = await upd_session.get(WedapImportDeliveryTask, task.id)
            if row is None:  # pragma: no cover - 并发删除，正常不发生
                continue
            row.attempts += 1
            if error is None:
                row.status = "DELIVERED"
                row.notified_at = now
                row.last_error = None
                terminal = "DELIVERED"
            elif row.attempts >= max_attempts:
                row.status = "FAILED"
                row.last_error = error
                terminal = "FAILED"
            else:
                row.status = "PENDING"
                row.next_retry_at = compute_next_retry(now, row.attempts, base_seconds=base_seconds)
                row.last_error = error
            await upd_session.commit()
        # 终态已持久化后再回执 recon（回执失败不回滚本地状态，下轮幂等重发）
        if terminal is not None and on_terminal is not None:
            await on_terminal(task, terminal, error)
        processed += 1

    return processed


class StagingChecksumMismatch(Exception):
    """staging 取回字节的 SHA-256 与任务记录的 file_checksum 不符（staging 损坏/被改）。"""


async def deliver_task(
    task: WedapImportDeliveryTask,
    *,
    s3_client: S3FileClient,
    wedap_client: WedapClient,
    staging_bucket: str,
    wedap_bucket: str,
) -> None:
    """生产投递动作：staging 取字节 → 校 checksum → deliver_batch（上传 wedap + 通知）。

    供 dispatch_delivery_once 的 deliver 参数绑定。staging 读后先校 SHA-256 == 任务记录值，
    防 staging 损坏把脏字节投给 wedap；deliver_batch 内部再校上传后 checksum（双重）。
    """
    content = await asyncio.to_thread(
        s3_client.get_bytes, bucket=staging_bucket, key=task.staging_key
    )
    actual = hashlib.sha256(content).hexdigest()
    if actual != task.file_checksum:
        raise StagingChecksumMismatch(f"{actual} != {task.file_checksum}")
    await deliver_batch(
        s3_client=s3_client,
        wedap_client=wedap_client,
        bucket=wedap_bucket,
        data_type=task.data_type,
        import_date=task.import_date,
        import_batch_no=task.import_batch_no,
        content=content,
        checksum=task.file_checksum,
        file_size=task.file_size,
        total_count=task.total_count,
    )
