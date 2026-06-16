import hashlib
import json
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditChainHead, AuditLog

GENESIS = "0" * 64


async def write_audit(
    session: AsyncSession,
    *,
    tenant_id: str,
    actor: str,
    action: str,
    entity: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """append-only 审计写入（per-tenant hash chain）。必须在调用方事务内执行。

    actor 必须来自认证上下文（svc:<caller>），禁止客户端伪造（规范 13）。

    链完整性靠 per-tenant 锚点行 `audit_chain_head` 串行化（A-m-003）：
    1. 幂等确保该 tenant 锚点行存在（普通读 + 缺则插，并发首写 dup 由 IntegrityError 吞掉）——
       不对不存在主键直接 FOR UPDATE，避免间隙锁触发 1213 死锁；
    2. 按主键 `FOR UPDATE` 锁定锚点行并**读最新已提交** last_row_hash 作 prev（用列级 select+
       execute 取标量，绕过 ORM identity-map 快照陈旧——否则锁不生效会分叉）；
    3. 写 audit_log 行并回写锚点 last_row_hash（core update，避免 ORM 缓存对象回写陈旧值）。
    锁随调用方事务提交释放，作用域限本 tenant：并发同 tenant 追加在锚点行排队，后写者读到前写者
    已提交的新链尾再续链，杜绝分叉。主键精确锁不产生间隙锁，故不会 1213 死锁。
    SQLite 忽略 FOR UPDATE（单测不受影响）。
    """
    # 1. 幂等确保锚点行存在
    exists = (
        await session.execute(
            select(AuditChainHead.tenant_id).where(AuditChainHead.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if exists is None:
        # 锚点不存在时 seed：必须从该 tenant **既有 audit_log 链尾**接续，而非硬编码 GENESIS。
        # 否则在升级前已有审计历史的库上，新链会从 GENESIS 重起 → 与历史末行断链（M1）。
        hist_tail = (
            await session.execute(
                select(AuditLog.row_hash)
                .where(AuditLog.tenant_id == tenant_id)
                .order_by(AuditLog.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        seed = hist_tail or GENESIS
        try:
            async with session.begin_nested():
                await session.execute(
                    insert(AuditChainHead).values(tenant_id=tenant_id, last_row_hash=seed)
                )
        except (
            IntegrityError
        ):  # pragma: no cover - 并发首写竞态：单线程测试不可达（MySQL 集成测覆盖）
            # 并发首写：对手已插入，下面 FOR UPDATE 会读到它
            pass

    # 2. 主键精确锁锚点行，读最新已提交 last_row_hash（列级 select 取标量，绕过 identity-map）
    prev = (
        await session.execute(
            select(AuditChainHead.last_row_hash)
            .where(AuditChainHead.tenant_id == tenant_id)
            .with_for_update()
        )
    ).scalar_one()

    # 3. 计算 row_hash → 写 audit_log → 回写锚点
    canonical = json.dumps(
        {
            "tenant": tenant_id,
            "actor": actor,
            "action": action,
            "entity": entity,
            "payload": payload,
            "prev": prev,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    row_hash = hashlib.sha256(canonical.encode()).hexdigest()
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor=actor,
            action=action,
            entity=entity,
            payload=payload,
            prev_hash=prev,
            row_hash=row_hash,
        )
    )
    await session.execute(
        update(AuditChainHead)
        .where(AuditChainHead.tenant_id == tenant_id)
        .values(last_row_hash=row_hash)
    )
