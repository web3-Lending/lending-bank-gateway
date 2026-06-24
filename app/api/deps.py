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


# per-currency 精度表（FU-GW-PER-CURRENCY-SCALE）：仅列 ISO-4217 偏离默认 2dp 的法币。
# gateway 仅收法币（currency 列 String(3)，token 走 custody 不经此），故全部 ≤4dp，
# Numeric(21,4) 列足以容纳——不需改列。未列币种按 _DEFAULT_CURRENCY_SCALE=2 校验。
_CURRENCY_SCALE: dict[str, int] = {
    # 0dp（无小数子单位）
    "BIF": 0,
    "CLP": 0,
    "DJF": 0,
    "GNF": 0,
    "ISK": 0,
    "JPY": 0,
    "KMF": 0,
    "KRW": 0,
    "PYG": 0,
    "RWF": 0,
    "UGX": 0,
    "VND": 0,
    "VUV": 0,
    "XAF": 0,
    "XOF": 0,
    "XPF": 0,
    # 3dp
    "BHD": 3,
    "IQD": 3,
    "JOD": 3,
    "KWD": 3,
    "LYD": 3,
    "OMR": 3,
    "TND": 3,
    # 4dp
    "CLF": 4,
    "UYW": 4,
}
_DEFAULT_CURRENCY_SCALE = 2  # ISO-4217 多数法币小数位
_COLUMN_MAX_SCALE = 4  # Numeric(21,4) 列存储兜底（无币种上下文时用，等同原 G5）


def parse_amount(raw: Any, currency: str | None = None) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise HTTPException(
            400,
            detail={"code": "GW_400_VALIDATION", "message": f"bad amount: {raw!r}"},
        ) from exc
    # 非有限值（NaN/sNaN/Infinity）是合法 Decimal 但金额非法：
    # NaN 会让下面的 `value <= 0` 比较抛 InvalidOperation（→ 未捕获 500）；
    # Infinity 能通过 `<= 0` 检查被当正数放行，最终在 Numeric(21,4) 落库时炸 500。
    # 统一在此显式拒为 400，避免脏金额穿透成 500。
    if not value.is_finite():
        raise HTTPException(
            400,
            detail={
                "code": "GW_400_VALIDATION",
                "message": f"amount must be finite: {raw!r}",
            },
        )
    if value <= 0:
        raise HTTPException(
            400,
            detail={
                "code": "GW_400_VALIDATION",
                "message": f"amount must be positive: {raw!r}",
            },
        )
    # scale（小数位）护栏——超精度输入会被 Numeric(21,4) 列静默 round，显式拒为 400。
    # value 已确保 finite（上方护栏），exponent 必为 int。
    ccy = (currency or "").strip().upper()
    if ccy:
        # 带币种：按该币种 ISO 精度校验「规范化有效位」（normalize 去尾零，
        # 容上游补零如 USD "100.0000"），只拦真·亚单位超精度（如 JPY 带小数）。
        max_scale = _CURRENCY_SCALE.get(ccy, _DEFAULT_CURRENCY_SCALE)
        actual_scale = max(0, -int(value.normalize().as_tuple().exponent))
        scale_label = f"{max_scale} decimal places for {ccy}"
    else:
        # 无币种上下文（如 distribute 内部聚合）：退回 G5 全局原始 ≤4dp 列兜底。
        max_scale = _COLUMN_MAX_SCALE
        actual_scale = -int(value.as_tuple().exponent)
        scale_label = "4 decimal places"
    if actual_scale > max_scale:
        raise HTTPException(
            400,
            detail={
                "code": "GW_400_VALIDATION",
                "message": f"amount scale exceeds {scale_label}: {raw!r}",
            },
        )
    return value


def validate_detail_consistency(
    body: dict[str, Any],
    *,
    total: Decimal | None,
    currency: str,
    detail_key: str,
    amount_field: str,
) -> None:
    """明细列表一致性前置校验（契约 C 透传原则：gateway 只校验不剪裁）。

    - detail_key 不在 body 中，或值为 None → 跳过（非强制明细场景；None 视同字段缺省）
    - 空列表 → 400 GW_400_VALIDATION "empty {detail_key}"
    - 各项含 currencyCode 且 != 顶层 currency → 400 "detail currency mismatch"
    - total 非 None 且各项都含 amount_field 时 sum != total → 400 "detail amount sum mismatch"
    - 明细项无 amount_field 字段 → 跳过 sum 校验（wedap 自动分配场景合法）
    - total=None → 无独立顶层总额（如 distribute 金额即取自明细 Σ），整段 sum 校验跳过，
      只保留「非空 + 币种一致」两项；避免对「明细自身求和再与自身比」的同义重复护栏
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

    # total=None：无独立顶层总额（distribute 金额取自明细 Σ），sum 校验是同义重复，整段跳过
    if total is None:
        return

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
