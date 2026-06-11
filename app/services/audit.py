import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog

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
    链完整性依赖串行写入；高并发可能分叉，验证工具需容忍并以叶节点反向追溯；
    v2 可加 SELECT FOR UPDATE
    """
    last = (
        await session.execute(
            select(AuditLog.row_hash)
            .where(AuditLog.tenant_id == tenant_id)
            .order_by(AuditLog.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    prev = last or GENESIS
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
