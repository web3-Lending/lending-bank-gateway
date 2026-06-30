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

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.s3 import S3FileClient
from app.clients.wedap import WedapClient
from app.models.wedap_delivery import WedapImportDeliveryTask
from app.services.wedap_import import deliver_batch
from app.services.wedap_import_result import ImportResult, parse_result

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


async def _reclaim_stale_sending(
    factory: async_sessionmaker[AsyncSession], *, now: dt.datetime, claim_timeout_seconds: float
) -> None:
    """崩溃残留的 SENDING（locked_at 超时）回 PENDING 重试（多副本 claim 防卡死）。"""
    cutoff = now - dt.timedelta(seconds=claim_timeout_seconds)
    async with factory() as session:
        await session.execute(
            update(WedapImportDeliveryTask)
            .where(
                WedapImportDeliveryTask.status == "SENDING",
                WedapImportDeliveryTask.locked_at < cutoff,
            )
            .values(status="PENDING", locked_at=None)
        )
        await session.commit()


async def _claim(factory: async_sessionmaker[AsyncSession], task_id: int, now: dt.datetime) -> bool:
    """原子 claim：PENDING→SENDING WHERE status='PENDING'。返回是否抢到（rowcount=1）。"""
    async with factory() as session:
        result = await session.execute(
            update(WedapImportDeliveryTask)
            .where(
                WedapImportDeliveryTask.id == task_id,
                WedapImportDeliveryTask.status == "PENDING",
            )
            .values(status="SENDING", locked_at=now)
        )
        await session.commit()
        return bool(result.rowcount == 1)  # type: ignore[attr-defined]  # CursorResult.rowcount


async def dispatch_delivery_once(
    factory: async_sessionmaker[AsyncSession],
    *,
    deliver: Callable[[WedapImportDeliveryTask], Awaitable[None]],
    now: dt.datetime,
    max_attempts: int = 5,
    base_seconds: float = 60.0,
    claim_timeout_seconds: float = 300.0,
    on_terminal: Callable[[WedapImportDeliveryTask, str, str | None], Awaitable[None]]
    | None = None,
) -> int:
    """扫一轮可投递任务并逐条投递，返回处理条数（=成功 claim 并处理的条数）。

    并发安全（codex 终审）：先 reclaim 崩溃残留 SENDING；snapshot PENDING 后对每条做原子 claim
    （PENDING→SENDING），只有抢到的副本继续 deliver，避免多副本重复南向投递。投递动作 deliver
    由调用方注入；状态机：成功→DELIVERED；失败→attempts+1，未达上限回 PENDING+退避，达上限→FAILED；
    终态/重排均清 locked_at。外呼在事务外，更新各自独立短事务。on_terminal 终态 commit 后触发回执。
    """
    await _reclaim_stale_sending(factory, now=now, claim_timeout_seconds=claim_timeout_seconds)

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
        # 原子 claim：抢不到（别的副本先抢）→ 跳过，绝不重复投递。
        if not await _claim(factory, task.id, now):
            continue

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
            row.locked_at = None  # 退出 SENDING，释放 claim
            if error is None:
                row.status = "DELIVERED"
                row.notified_at = (
                    now  # = wedap notify 成功时刻（非"已回执 recon"，回执是 on_terminal 另发）
                )
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


async def mark_callback_sent(
    factory: async_sessionmaker[AsyncSession], task_id: int, now: dt.datetime
) -> None:
    """原子标记 gateway→recon 回执已送达（callback_sent_at），消除 recon 卡 ENQUEUED；同时清锁。"""
    async with factory() as session:
        await session.execute(
            update(WedapImportDeliveryTask)
            .where(WedapImportDeliveryTask.id == task_id)
            .values(callback_sent_at=now, callback_locked_at=None)
        )
        await session.commit()


async def _claim_callback(
    factory: async_sessionmaker[AsyncSession],
    task_id: int,
    now: dt.datetime,
    *,
    lock_timeout_seconds: float,
) -> bool:
    """原子 claim 一条未送达回执供重发（codex 复评 P1）：set callback_locked_at=now WHERE
    callback_sent_at 为空 且(锁空或超时)。多实例并发只一个 rowcount=1 抢到。返回是否抢到。"""
    cutoff = now - dt.timedelta(seconds=lock_timeout_seconds)
    async with factory() as session:
        result = await session.execute(
            update(WedapImportDeliveryTask)
            .where(
                WedapImportDeliveryTask.id == task_id,
                WedapImportDeliveryTask.callback_sent_at.is_(None),
                or_(
                    WedapImportDeliveryTask.callback_locked_at.is_(None),
                    WedapImportDeliveryTask.callback_locked_at < cutoff,
                ),
            )
            .values(callback_locked_at=now)
        )
        await session.commit()
        return bool(result.rowcount == 1)  # type: ignore[attr-defined]  # CursorResult.rowcount


async def resend_pending_callbacks_once(
    factory: async_sessionmaker[AsyncSession],
    *,
    send: Callable[[WedapImportDeliveryTask, str, str | None], Awaitable[None]],
    now: dt.datetime,
    limit: int = 100,
    lock_timeout_seconds: float = 300.0,
) -> int:
    """重发未送达回执（终态 DELIVERED/FAILED 且 callback_sent_at 为空）→ durable 兜底。返回重发数。

    并发安全（codex 复评 P1）：每条先原子 claim（callback_locked_at）抢到才重发，多实例只一个
    命中；send 成功 mark_callback_sent（callback_sent_at 置位、排除后续扫描），失败留锁，残留锁
    超时（lock_timeout_seconds）由下轮 claim 重新抢占。send 由调用方注入（best-effort）。
    """
    async with factory() as snap:
        tasks = list(
            (
                await snap.execute(
                    select(WedapImportDeliveryTask)
                    .where(
                        WedapImportDeliveryTask.status.in_(("DELIVERED", "FAILED")),
                        WedapImportDeliveryTask.callback_sent_at.is_(None),
                        or_(
                            WedapImportDeliveryTask.callback_locked_at.is_(None),
                            WedapImportDeliveryTask.callback_locked_at
                            < now - dt.timedelta(seconds=lock_timeout_seconds),
                        ),
                    )
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    resent = 0
    for task in tasks:
        if not await _claim_callback(
            factory, task.id, now, lock_timeout_seconds=lock_timeout_seconds
        ):
            continue
        await send(task, task.status, task.last_error)
        resent += 1
    return resent


async def _claim_result(
    factory: async_sessionmaker[AsyncSession],
    task_id: int,
    now: dt.datetime,
    *,
    lock_timeout_seconds: float,
) -> bool:
    """原子 claim 一条待回收结果供拉取+转投：set result_locked_at=now WHERE result_collected_at
    为空 且(锁空或超时)。多实例并发只一个 rowcount=1 抢到。返回是否抢到。"""
    cutoff = now - dt.timedelta(seconds=lock_timeout_seconds)
    async with factory() as session:
        result = await session.execute(
            update(WedapImportDeliveryTask)
            .where(
                WedapImportDeliveryTask.id == task_id,
                WedapImportDeliveryTask.result_collected_at.is_(None),
                or_(
                    WedapImportDeliveryTask.result_locked_at.is_(None),
                    WedapImportDeliveryTask.result_locked_at < cutoff,
                ),
            )
            .values(result_locked_at=now)
        )
        await session.commit()
        return bool(result.rowcount == 1)  # type: ignore[attr-defined]  # CursorResult.rowcount


async def _release_result_lock(factory: async_sessionmaker[AsyncSession], task_id: int) -> None:
    """result 未就绪(404)时立即释放 claim 锁，让下轮尽快重试（不等锁超时）。"""
    async with factory() as session:
        await session.execute(
            update(WedapImportDeliveryTask)
            .where(WedapImportDeliveryTask.id == task_id)
            .values(result_locked_at=None)
        )
        await session.commit()


async def mark_result_collected(
    factory: async_sessionmaker[AsyncSession], task_id: int, now: dt.datetime
) -> None:
    """原子标记 _result.json 已回收并转投 recon（result_collected_at），停止轮询；清锁。"""
    async with factory() as session:
        await session.execute(
            update(WedapImportDeliveryTask)
            .where(WedapImportDeliveryTask.id == task_id)
            .values(result_collected_at=now, result_locked_at=None)
        )
        await session.commit()


async def collect_results_once(
    factory: async_sessionmaker[AsyncSession],
    *,
    fetch: Callable[[WedapImportDeliveryTask], Awaitable[bytes | None]],
    post: Callable[[WedapImportDeliveryTask, ImportResult], Awaitable[None]],
    now: dt.datetime,
    limit: int = 100,
    lock_timeout_seconds: float = 300.0,
) -> int:
    """回收 _result.json（DELIVERED 且 result_collected_at 空）→ 转投 recon。返回回收数。

    并发安全：每条先原子 claim（result_locked_at）抢到才拉取，多实例只一个命中。fetch 返回
    None = _result.json 未就绪（wedap 仍在处理）→ 立即释放锁待下轮；返回字节 → parse_result
    解析、post 转投 recon 逐条异常行、mark_result_collected 置位排除后续扫描。fetch/post 由
    dispatcher 注入（绑 S3/recon 客户端），保持本服务纯逻辑可测。
    """
    async with factory() as snap:
        tasks = list(
            (
                await snap.execute(
                    select(WedapImportDeliveryTask)
                    .where(
                        WedapImportDeliveryTask.status == "DELIVERED",
                        WedapImportDeliveryTask.result_collected_at.is_(None),
                        or_(
                            WedapImportDeliveryTask.result_locked_at.is_(None),
                            WedapImportDeliveryTask.result_locked_at
                            < now - dt.timedelta(seconds=lock_timeout_seconds),
                        ),
                    )
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    collected = 0
    for task in tasks:
        if not await _claim_result(
            factory, task.id, now, lock_timeout_seconds=lock_timeout_seconds
        ):
            continue
        try:
            raw = await fetch(task)
            if raw is None:  # _result.json 未就绪 → 释放锁，下轮重试
                await _release_result_lock(factory, task.id)
                continue
            await post(task, parse_result(raw))
        except Exception as exc:  # noqa: BLE001 - 单条失败不崩循环；释放锁待下轮重试（post 幂等）
            logger.warning(
                "wedap_delivery: 回收 _result.json 失败 batch=%s err=%s（释放锁待重试）",
                task.import_batch_no,
                exc,
            )
            await _release_result_lock(factory, task.id)
            continue
        await mark_result_collected(factory, task.id, now)
        collected += 1
    return collected
