import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.idempotency import IdempotencyRecord


class IdempotencyConflict(Exception):
    """同 key 不同 payload —— 北向必须回 409。"""


def payload_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def check_or_register(
    session: AsyncSession,
    *,
    tenant_id: str,
    business_scope: str,
    idempotency_key: str,
    method: str,
    path: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    h = payload_hash(payload)
    row = (
        await session.execute(
            select(IdempotencyRecord).where(
                IdempotencyRecord.tenant_id == tenant_id,
                IdempotencyRecord.business_scope == business_scope,
                IdempotencyRecord.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        session.add(
            IdempotencyRecord(
                tenant_id=tenant_id,
                business_scope=business_scope,
                idempotency_key=idempotency_key,
                method=method,
                path=path,
                payload_hash=h,
            )
        )
        await session.flush()
        return None
    if row.payload_hash != h:
        raise IdempotencyConflict(idempotency_key)
    return row.first_response


async def record_response(
    session: AsyncSession,
    *,
    tenant_id: str,
    business_scope: str,
    idempotency_key: str,
    response: dict[str, Any],
    final_effect_id: str | None = None,
) -> None:
    row = (
        await session.execute(
            select(IdempotencyRecord).where(
                IdempotencyRecord.tenant_id == tenant_id,
                IdempotencyRecord.business_scope == business_scope,
                IdempotencyRecord.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one()
    row.first_response = response
    row.final_effect_id = final_effect_id
