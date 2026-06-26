"""wedap flow-import 投递服务（§4.3「A+异步回执」· gateway 侧）。

- enqueue_delivery：recon enqueue 交单 → 插一条 PENDING 投递任务（幂等）。
- dispatch_delivery_once：dispatcher 扫 PENDING/可重试任务 → 取 staging 字节 → deliver_batch
  （S3 upload + checksum verify + wedap notify）→ DELIVERED/FAILED + 退避重试。

权威业务状态在 recon 的 wedap_export_batch；本表只是执行账本。投递终态由调用方经回执回写 recon。
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.wedap_delivery import WedapImportDeliveryTask


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
