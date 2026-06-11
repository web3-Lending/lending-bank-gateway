"""FastAPI 依赖：header 校验 + 金额解析。"""

from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import HTTPException, Request

from app.core.context import current_ids


def assert_idempotency_key_matches(request: Request, biz_seq_no: str) -> None:
    """若请求携带 Idempotency-Key header，校验其值必须与 bizSeqNo 一致。

    - header 不存在或为空 → 放行（以 bizSeqNo 为准）
    - header 存在且非空但与 biz_seq_no 不一致 → 400 GW_400_IDEMPOTENCY_KEY
    """
    key = request.headers.get("Idempotency-Key", "").strip()
    if key and key != biz_seq_no:
        raise HTTPException(
            400,
            detail={
                "code": "GW_400_IDEMPOTENCY_KEY",
                "message": "Idempotency-Key mismatch with bizSeqNo",
            },
        )


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
