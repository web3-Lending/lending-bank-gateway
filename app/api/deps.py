"""FastAPI 依赖：header 校验 + 金额解析。"""

from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import HTTPException, Request

from app.core.context import current_ids


def require_headers(request: Request) -> dict[str, str]:
    ids = current_ids()
    if not ids.tenant_id:
        raise HTTPException(400, detail={"code": "GW_400_HEADER", "message": "missing X-Tenant-Id"})
    if not ids.request_id:
        raise HTTPException(
            400, detail={"code": "GW_400_HEADER", "message": "missing X-Request-Id"}
        )
    return {
        "tenant_id": ids.tenant_id,
        "request_id": ids.request_id,
        "trace_id": ids.trace_id,
        "caller_service": request.headers.get("X-Caller-Service", "unknown"),
    }


def parse_amount(raw: Any) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise HTTPException(
            400,
            detail={"code": "GW_400_VALIDATION", "message": f"bad amount: {raw!r}"},
        ) from exc
    if value <= 0:
        raise HTTPException(
            400,
            detail={
                "code": "GW_400_VALIDATION",
                "message": f"amount must be positive: {raw!r}",
            },
        )
    return value
