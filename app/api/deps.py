"""FastAPI 依赖：header 校验 + 金额解析 + 明细一致性前置校验。"""

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


def validate_detail_consistency(
    body: dict[str, Any],
    *,
    total: Decimal,
    currency: str,
    detail_key: str,
    amount_field: str,
) -> None:
    """明细列表一致性前置校验（契约 C 透传原则：gateway 只校验不剪裁）。

    - detail_key 不在 body 中，或值为 None → 跳过（非强制明细场景；None 视同字段缺省）
    - 空列表 → 400 GW_400_VALIDATION "empty {detail_key}"
    - 各项含 amount_field 时 sum(Decimal) != total → 400 "detail amount sum mismatch"
    - 各项含 currencyCode 且 != 顶层 currency → 400 "detail currency mismatch"
    - 明细项无 amount_field 字段 → 跳过 sum 校验（wedap 自动分配场景合法）
    """
    if detail_key not in body or body[detail_key] is None:
        return

    items: list[Any] = body[detail_key]
    if not items:
        raise HTTPException(
            400,
            detail={
                "code": "GW_400_VALIDATION",
                "message": f"empty {detail_key}",
            },
        )

    # currency mismatch 校验（先于 sum，尽早拦截）
    for item in items:
        if not isinstance(item, dict):
            continue
        item_currency = item.get("currencyCode")
        if item_currency is not None and item_currency != currency:
            raise HTTPException(
                400,
                detail={
                    "code": "GW_400_VALIDATION",
                    "message": "detail currency mismatch",
                },
            )

    # sum 校验：只有所有项都含 amount_field 时才校验（部分缺失=wedap 自动分配，跳过）
    amounts: list[Decimal] = []
    has_amount = True
    for item in items:
        if not isinstance(item, dict) or amount_field not in item:
            has_amount = False
            break
        try:
            amounts.append(Decimal(str(item[amount_field])))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise HTTPException(
                400,
                detail={
                    "code": "GW_400_VALIDATION",
                    "message": f"invalid {amount_field} in {detail_key} item",
                },
            ) from exc

    if has_amount and sum(amounts) != total:
        raise HTTPException(
            400,
            detail={
                "code": "GW_400_VALIDATION",
                "message": "detail amount sum mismatch",
            },
        )
