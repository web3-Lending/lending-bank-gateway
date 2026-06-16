"""outbox 转发服务：enqueue / dispatch_once / replay_dead。

设计要点（规范 14）：
- 外呼（HTTP POST）在事务外执行，避免长事务持锁
- dispatch_once 先读 snapshot（无事务），再逐条外呼，再开独立事务更新状态
- backoff：FAILED 时写 next_retry_at = now + base * 2^(attempts-1)
- worker 上下文无 HTTP 中间件，日志用 outbox id 关联（trace_id=outbox-<id>）
"""

import datetime as dt
import logging
from typing import Any

import httpx
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.callback import CallbackOutbox

logger = logging.getLogger(__name__)


async def enqueue_forward(
    session: AsyncSession,
    *,
    tenant_id: str,
    target: str,
    payload: dict[str, Any],
    dedup_key: str | None = None,
    trace_id: str | None = None,
) -> CallbackOutbox:
    """在当前 session（调用方负责事务）插入一条 PENDING outbox 行并返回。

    dedup_key 非 None 时先查 (tenant_id, target, dedup_key) 是否已存在：
    - 已存在 → 返回既有行，不重插（幂等）
    - 不存在 → 正常插入；若并发竞态触发 IntegrityError，begin_nested 捕获后查回既有行
    dedup_key 为 None 时退化为无幂等保证的普通插入（向后兼容）。
    """
    if dedup_key is not None:
        # 先查是否已存在（乐观路径，无锁）
        existing = (
            await session.execute(
                select(CallbackOutbox).where(
                    CallbackOutbox.tenant_id == tenant_id,
                    CallbackOutbox.target == target,
                    CallbackOutbox.dedup_key == dedup_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        # 尝试插入；并发竞态下唯一约束可能 IntegrityError
        try:
            async with session.begin_nested():
                row = CallbackOutbox(
                    tenant_id=tenant_id,
                    target=target,
                    payload=payload,
                    dedup_key=dedup_key,
                    trace_id=trace_id,
                )
                session.add(row)
                await session.flush()
            return row
        except (
            IntegrityError
        ):  # pragma: no cover - 并发竞态：两线程同时通过乐观查询后触发，单线程测试不可达
            # 并发插入被约束拒绝，查回既有行
            existing = (
                await session.execute(
                    select(CallbackOutbox).where(
                        CallbackOutbox.tenant_id == tenant_id,
                        CallbackOutbox.target == target,
                        CallbackOutbox.dedup_key == dedup_key,
                    )
                )
            ).scalar_one()
            return existing
    else:
        row = CallbackOutbox(tenant_id=tenant_id, target=target, payload=payload, trace_id=trace_id)
        session.add(row)
        await session.flush()
        return row


async def _reclaim_stale_sending(
    factory: async_sessionmaker[AsyncSession], *, now: dt.datetime, claim_timeout_seconds: float
) -> None:
    """把卡死在 SENDING 超过 claim_timeout 的行回收为 FAILED（上一轮 dispatcher 外呼后崩溃）。

    locked_at < now - timeout 才回收，避免误抢正在外呼的行。回收为 FAILED（attempts 不变），
    下一轮按 backoff 重投。
    """
    cutoff = now - dt.timedelta(seconds=claim_timeout_seconds)
    async with factory() as session:
        async with session.begin():
            await session.execute(
                update(CallbackOutbox)
                .where(
                    CallbackOutbox.status == "SENDING",
                    CallbackOutbox.locked_at.is_not(None),
                    CallbackOutbox.locked_at < cutoff,
                )
                .values(status="FAILED", last_error="reclaimed_stale_sending")
            )


async def dispatch_once(
    factory: async_sessionmaker[AsyncSession],
    *,
    targets: dict[str, str],
    max_attempts: int,
    backoff_base_seconds: float = 2.0,
    claim_timeout_seconds: float = 300.0,
) -> int:
    """投递一轮到期行；返回实际处理条数。

    多副本安全（A-M-001）：每条行先**原子 claim**（条件 UPDATE 置 SENDING + locked_at，
    rowcount==1 才拥有），再事务外外呼，再独立事务终结状态。正常运行下两副本同轮只有一个能
    claim 成功，消除「同轮重复 claim + attempts 双计」。外呼后崩溃留下的 SENDING 行由
    _reclaim_stale_sending 超时回收。

    投递语义为 **at-least-once**（非 exactly-once）：极端边界——副本 A claim 后外呼 stall 超过
    claim_timeout_seconds（默认 300s，远大于 10s 外呼 timeout）→ 副本 B reclaim 并重投 → A 迟到
    成功 = 下游收到两次。最终一致性依赖**下游按 X-Request-Id(=dedup_key) 幂等**兜底。
    （CAS/claim-token 加固该边界见 followup。）httpx client 整轮复用（CR-m-002）；
    X-Trace-Id 透传原始 trace_id（A-m-001）。
    """
    now = dt.datetime.now(dt.UTC)

    # 0. 回收上一轮卡死的 SENDING
    await _reclaim_stale_sending(factory, now=now, claim_timeout_seconds=claim_timeout_seconds)

    # 1. 候选 id（PENDING/FAILED 且到期）
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(CallbackOutbox).where(CallbackOutbox.status.in_(("PENDING", "FAILED")))
                )
            )
            .scalars()
            .all()
        )
        due_ids = [
            r.id
            for r in rows
            if r.next_retry_at is None or r.next_retry_at.replace(tzinfo=dt.UTC) <= now
        ]

    # 2. 逐条 claim → 外呼（复用 client）→ 终结状态
    handled = 0
    async with httpx.AsyncClient(timeout=10.0) as client:
        for oid in due_ids:
            # 2a. 原子 claim：仅当仍 PENDING/FAILED 且到期才置 SENDING（多副本只有一个 rowcount==1）
            async with factory() as session:
                async with session.begin():
                    result = await session.execute(
                        update(CallbackOutbox)
                        .where(
                            CallbackOutbox.id == oid,
                            CallbackOutbox.status.in_(("PENDING", "FAILED")),
                            or_(
                                CallbackOutbox.next_retry_at.is_(None),
                                CallbackOutbox.next_retry_at <= now,
                            ),
                        )
                        .values(status="SENDING", locked_at=dt.datetime.now(dt.UTC))
                    )
                    claimed = result.rowcount  # type: ignore[attr-defined]  # CursorResult.rowcount
            if (
                claimed != 1
            ):  # pragma: no cover - 多副本 claim 竞态：单线程测试不可达（MySQL 集成测覆盖）
                # 被别的副本抢走，或状态已变 → 跳过
                continue

            # 2b. 读已 claim 的行数据
            async with factory() as session:
                row = await session.get(CallbackOutbox, oid)
                if row is None:  # pragma: no cover - claim 成功后理论不可达
                    continue
                tenant_id, target, payload = row.tenant_id, row.target, row.payload
                dedup_key, trace_id = row.dedup_key, row.trace_id

            url = targets.get(target)
            ok_flag, error = False, None
            # X-Request-Id 用 dedup_key（下游幂等键跨重放稳定），无则降级 outbox-{id}
            request_id = dedup_key if dedup_key is not None else f"outbox-{oid}"
            # X-Trace-Id 透传原始 trace_id（A-m-001），无则降级 outbox-{id}
            fwd_trace = trace_id if trace_id else f"outbox-{oid}"

            if url is None:
                error = f"no url for target {target}"
            else:
                try:
                    resp = await client.post(
                        url,
                        json=payload,
                        headers={
                            "X-Caller-Service": "lending-bank-gateway",
                            "X-Tenant-Id": tenant_id,
                            "X-Trace-Id": fwd_trace,
                            "X-Request-Id": request_id,
                        },
                    )
                    ok_flag = resp.status_code < 300
                    error = None if ok_flag else f"http {resp.status_code}"
                except httpx.HTTPError as exc:
                    error = type(exc).__name__

            # 2c. 终结状态（清 locked_at）
            async with factory() as session:
                async with session.begin():
                    row = await session.get(CallbackOutbox, oid)
                    if row is None:  # pragma: no cover - 理论不可达
                        continue
                    row.attempts += 1
                    row.locked_at = None
                    if ok_flag:
                        row.status = "SENT"
                    elif row.attempts >= max_attempts:
                        row.status = "DEAD"
                        row.last_error = error
                        logger.error(
                            "outbox %s DEAD after %s attempts: %s", oid, row.attempts, error
                        )
                    else:
                        row.status = "FAILED"
                        row.last_error = error
                        row.next_retry_at = dt.datetime.now(dt.UTC) + dt.timedelta(
                            seconds=backoff_base_seconds * (2 ** (row.attempts - 1))
                        )
            handled += 1

    return handled


async def replay_dead(session: AsyncSession, *, outbox_id: int) -> bool:
    """将 DEAD 行重置为 PENDING（attempts 清零）。

    调用方负责事务；行不存在或非 DEAD 状态返回 False。
    """
    row = await session.get(CallbackOutbox, outbox_id)
    if row is None or row.status != "DEAD":
        return False
    row.status = "PENDING"
    row.attempts = 0
    row.next_retry_at = None
    return True
