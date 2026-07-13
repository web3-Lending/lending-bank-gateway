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
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.s3 import S3FileClient
from app.clients.wedap import ACCEPTED_BATCH_STATUS, WedapClient, _error_text
from app.models.wedap_delivery import WedapImportDeliveryTask
from app.models.wedap_delivery_alert import WedapDeliveryAlert
from app.services.wedap_import import WedapBatchRejected, deliver_batch
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


def compute_result_deadline(
    now: dt.datetime, *, anchor_hour: int, grace_minutes: float
) -> dt.datetime:
    """§6.1 护栏②：result 回收截止 = ``now`` 之后下一个 wedap scanner 运行点 + grace。

    anchor_hour 是 wedap BatchScanScheduler 每日 cron 的 UTC 小时（默认 2 = ``0 0 2 * * ?``）。
    受理正好落在 anchor 时刻上时取次日窗口（scanner 与受理的先后不可知，保守多等一天）。
    """
    anchor = now.replace(hour=anchor_hour, minute=0, second=0, microsecond=0)
    if anchor <= now:
        anchor += dt.timedelta(days=1)
    return anchor + dt.timedelta(minutes=grace_minutes)


async def dispatch_delivery_once(
    factory: async_sessionmaker[AsyncSession],
    *,
    deliver: Callable[[WedapImportDeliveryTask], Awaitable[dict[str, Any] | None]],
    now: dt.datetime,
    max_attempts: int = 5,
    base_seconds: float = 60.0,
    claim_timeout_seconds: float = 300.0,
    on_terminal: Callable[[WedapImportDeliveryTask, str, str | None], Awaitable[None]]
    | None = None,
    result_deadline: Callable[[dt.datetime], dt.datetime] | None = None,
    clock: Callable[[], dt.datetime] | None = None,
) -> int:
    """扫一轮可投递任务并逐条投递，返回处理条数（=成功 claim 并处理的条数）。

    并发安全（codex 终审）：先 reclaim 崩溃残留 SENDING；snapshot PENDING 后对每条做原子 claim
    （PENDING→SENDING），只有抢到的副本继续 deliver，避免多副本重复南向投递。投递动作 deliver
    由调用方注入；状态机：成功→DELIVERED；失败→attempts+1，未达上限回 PENDING+退避，达上限→FAILED；
    终态/重排均清 locked_at。外呼在事务外，更新各自独立短事务。on_terminal 终态 commit 后触发回执。

    §6.1 护栏②：deliver 成功（= wedap 受理，非受理已在 deliver_task 抛出）时额外落
    accepted_at、响应回带的 result_file_path、result_deadline_at=result_deadline(accepted_at)
    （未注入 result_deadline 则留空，不影响既有路径）。受理时刻取 ``clock()``（默认真实
    UTC now，测试可注入固定时钟）而非本轮扫描起始 ``now``——投递外呼耗时可能跨过 scanner
    anchor，用扫描起始时刻算 deadline 会把截止算早一个窗口，制造假 RESULT_OVERDUE
    （codex HIGH）。notified_at 同步用受理时刻。
    """
    real_clock = clock if clock is not None else lambda: dt.datetime.now(dt.UTC)
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
        response: dict[str, Any] | None = None
        try:
            response = await deliver(task)
        except Exception as exc:  # noqa: BLE001 - 投递失败统一进退避，错误存 last_error
            error = str(exc)
            logger.warning(
                "wedap_delivery: 投递失败 batch=%s attempt=%s err=%s",
                task.import_batch_no,
                task.attempts + 1,
                error,
            )
        # 受理时刻在 deliver 返回后取（外呼耗时可能跨 scanner anchor，codex HIGH）。
        accepted_now = real_clock()

        terminal: str | None = None
        async with factory() as upd_session:
            row = await upd_session.get(WedapImportDeliveryTask, task.id)
            if row is None:  # pragma: no cover - 并发删除，正常不发生
                continue
            row.attempts += 1
            row.locked_at = None  # 退出 SENDING，释放 claim
            if error is None:
                row.status = "DELIVERED"
                # wedap notify 成功时刻（非"已回执 recon"，回执是 on_terminal 另发）。
                row.notified_at = accepted_now
                # §6.1 护栏②：deliver 无异常 = wedap 受理（非受理 status 已在 deliver_task 抛出）。
                row.accepted_at = accepted_now
                if response is not None:
                    rfp = response.get("resultFilePath")
                    row.result_file_path = rfp if isinstance(rfp, str) else None
                if result_deadline is not None:
                    row.result_deadline_at = result_deadline(accepted_now)
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
    presigned_enabled: bool = False,
) -> dict[str, Any]:
    """生产投递动作：staging 取字节 → 校 checksum → deliver_batch（上传 wedap + 通知）。

    供 dispatch_delivery_once 的 deliver 参数绑定。staging 读（lending 自有 bucket，boto3）后先校
    SHA-256 == 任务记录值，防 staging 损坏把脏字节投给 wedap；deliver_batch 内部再校上传后
    checksum（双重）。presigned_enabled 透传给 deliver_batch，决定 wedap 侧上传走 presigned PUT
    还是 boto3 直传（staging 读始终 boto3，不受影响）。

    §6.1 护栏②：notify 响应 status 非受理类（ACCEPTED_BATCH_STATUS 之外）→ 抛 WedapBatchRejected
    进 dispatch 退避重试路径，不再把业务拒绝误记 DELIVERED；受理 → 返回响应体（含
    resultFilePath），由 dispatch 落库留证。
    """
    content = await asyncio.to_thread(
        s3_client.get_bytes, bucket=staging_bucket, key=task.staging_key
    )
    actual = hashlib.sha256(content).hexdigest()
    if actual != task.file_checksum:
        raise StagingChecksumMismatch(f"{actual} != {task.file_checksum}")
    response = await deliver_batch(
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
        presigned_enabled=presigned_enabled,
    )
    status = str(response.get("status"))
    if status not in ACCEPTED_BATCH_STATUS:
        # 拒因文案走 msg|message 双字段兼容（baffle=msg，真 wedap=message），
        # 与 client 层 _error_text 同口径，防 last_error 落成字面量 "None"。
        raise WedapBatchRejected(status, _error_text(response))
    return response


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
    token: str,
    *,
    lock_timeout_seconds: float,
) -> bool:
    """原子 claim 一条待回收结果：set result_locked_at=now + result_lock_token=token WHERE
    result_collected_at 为空 且(锁空或超时)。多实例并发只一个 rowcount=1 抢到。返回是否抢到。"""
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
            .values(result_locked_at=now, result_lock_token=token)
        )
        await session.commit()
        return bool(result.rowcount == 1)  # type: ignore[attr-defined]  # CursorResult.rowcount


async def _release_result_lock(
    factory: async_sessionmaker[AsyncSession], task_id: int, token: str
) -> None:
    """释放本轮 claim 的锁（result 未就绪/失败时立即放，不等超时）。

    ownership guard：仅当 result_lock_token == 本轮 token 且未置位才清；锁已被别的实例重抢
    （超时后换了 token）时 no-op，绝不清掉新 owner 的锁（codex P1 二轮 HIGH-1，token 防 MySQL
    DATETIME 截微秒致时间戳等值永不匹配）。
    """
    async with factory() as session:
        await session.execute(
            update(WedapImportDeliveryTask)
            .where(
                WedapImportDeliveryTask.id == task_id,
                WedapImportDeliveryTask.result_lock_token == token,
                WedapImportDeliveryTask.result_collected_at.is_(None),
            )
            .values(result_locked_at=None, result_lock_token=None)
        )
        await session.commit()


async def mark_result_collected(
    factory: async_sessionmaker[AsyncSession], task_id: int, token: str, now: dt.datetime
) -> bool:
    """标记 _result.json 已回收并转投 recon（result_collected_at=now），停轮询清锁。

    ownership guard：仅当 result_lock_token == 本轮 token 且未置位才置；锁已被别的实例重抢时
    no-op 返回 False，不覆盖新 owner（codex P1 二轮 HIGH-1）。返回是否本轮真正置位。
    """
    async with factory() as session:
        result = await session.execute(
            update(WedapImportDeliveryTask)
            .where(
                WedapImportDeliveryTask.id == task_id,
                WedapImportDeliveryTask.result_lock_token == token,
                WedapImportDeliveryTask.result_collected_at.is_(None),
            )
            .values(result_collected_at=now, result_locked_at=None, result_lock_token=None)
        )
        await session.commit()
        return bool(result.rowcount == 1)  # type: ignore[attr-defined]  # CursorResult.rowcount


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
        token = uuid.uuid4().hex  # 本轮 ownership token，mark/release 按它确认锁仍属本轮
        if not await _claim_result(
            factory, task.id, now, token, lock_timeout_seconds=lock_timeout_seconds
        ):
            continue
        try:
            raw = await fetch(task)
            if raw is None:  # _result.json 未就绪 → 释放锁，下轮重试
                await _release_result_lock(factory, task.id, token)
                continue
            await post(task, parse_result(raw))
        except Exception as exc:  # noqa: BLE001 - 单条失败不崩循环；释放锁待下轮重试（post 幂等）
            logger.warning(
                "wedap_delivery: 回收 _result.json 失败 batch=%s err=%s（释放锁待重试）",
                task.import_batch_no,
                exc,
            )
            await _release_result_lock(factory, task.id, token)
            continue
        if await mark_result_collected(factory, task.id, token, now):  # 仅本轮真置位才计数
            collected += 1
    return collected


async def _record_delivery_alert(
    factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: str,
    import_batch_no: str,
    kind: str,
    detail: str,
    now: dt.datetime,
) -> bool:
    """插入投递告警标记；UNIQUE(tenant, batch, kind) 命中（已告警过）→ 返 False。

    INSERT + 捕获 IntegrityError 跨 SQLite/MySQL 可移植地实现「只告警一次」去重
    （同 OrderStuckAlert 的 _record_stuck_alert 模式）。
    """
    try:
        async with factory() as session:
            async with session.begin():
                session.add(
                    WedapDeliveryAlert(
                        tenant_id=tenant_id,
                        import_batch_no=import_batch_no,
                        kind=kind,
                        detail=detail[:255],
                        first_alerted_at=now,
                    )
                )
        return True
    except IntegrityError:
        return False


async def alert_stuck_deliveries(
    factory: async_sessionmaker[AsyncSession],
    *,
    now: dt.datetime,
    pending_max_age_seconds: float,
    batch_limit: int = 100,
) -> int:
    """§6.1 护栏③④：wedap 投递 silent-failure 去重告警。返回本轮新增告警数。

    - PENDING_STUCK（护栏③）：停留 PENDING/SENDING 超 pending_max_age 仍未投出——重试打满
      退避窗或 worker 停摆都会落进来，不静默排队。
    - RESULT_OVERDUE（护栏④）：已受理（DELIVERED）但超过 result_deadline_at 仍未回收
      _result.json——切流负责人须按 §6.1 评估回滚（deadline 为空的存量行不判，避免误报）。
    同批同类只告警一次（唯一约束去重 + 限频 ERROR），admin /wedap-import/delivery-report 可查。
    """
    new_alerts = 0

    min_created = now - dt.timedelta(seconds=pending_max_age_seconds)
    async with factory() as session:
        stuck = (
            (
                await session.execute(
                    select(WedapImportDeliveryTask)
                    .where(
                        WedapImportDeliveryTask.status.in_(("PENDING", "SENDING")),
                        WedapImportDeliveryTask.created_at <= min_created,
                    )
                    .limit(batch_limit)
                )
            )
            .scalars()
            .all()
        )
    for task in stuck:
        # SQLite/aiomysql 读回的 DATETIME 可能是 naive（按 UTC 存）→ 补 tzinfo 再算 age。
        created = (
            task.created_at
            if task.created_at.tzinfo is not None
            else task.created_at.replace(tzinfo=dt.UTC)
        )
        age = int((now - created).total_seconds())
        if await _record_delivery_alert(
            factory,
            tenant_id=task.tenant_id,
            import_batch_no=task.import_batch_no,
            kind="PENDING_STUCK",
            detail=f"status={task.status} attempts={task.attempts} age_s={age}",
            now=now,
        ):
            new_alerts += 1
            logger.error(
                "wedap delivery stuck over max_age %s/%s status=%s attempts=%s"
                "（已记 wedap_delivery_alert · §6.1 护栏③）",
                task.tenant_id,
                task.import_batch_no,
                task.status,
                task.attempts,
            )

    async with factory() as session:
        overdue = (
            (
                await session.execute(
                    select(WedapImportDeliveryTask)
                    .where(
                        WedapImportDeliveryTask.status == "DELIVERED",
                        WedapImportDeliveryTask.result_collected_at.is_(None),
                        WedapImportDeliveryTask.result_deadline_at.is_not(None),
                        WedapImportDeliveryTask.result_deadline_at <= now,
                    )
                    .limit(batch_limit)
                )
            )
            .scalars()
            .all()
        )
    for task in overdue:
        deadline = task.result_deadline_at
        if deadline is None:  # pragma: no cover - WHERE 已滤空，防御性判空给 mypy
            continue
        if await _record_delivery_alert(
            factory,
            tenant_id=task.tenant_id,
            import_batch_no=task.import_batch_no,
            kind="RESULT_OVERDUE",
            detail=f"deadline={deadline.isoformat()} accepted_at="
            f"{task.accepted_at.isoformat() if task.accepted_at else None}",
            now=now,
        ):
            new_alerts += 1
            logger.error(
                "wedap result overdue（超截止未回收 _result.json）%s/%s deadline=%s"
                "（已记 wedap_delivery_alert · §6.1 护栏④，评估切流回滚）",
                task.tenant_id,
                task.import_batch_no,
                deadline.isoformat(),
            )
    return new_alerts
