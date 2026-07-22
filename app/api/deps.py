"""FastAPI 依赖：header 校验 + 金额解析 + 明细一致性前置校验。"""

import datetime as dt
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request

from app.core.amounts import MAX_INTEGER_DIGITS, AmountGuardError, parse_guarded_decimal
from app.core.context import current_ids


def bank_req_date(request: Request) -> str:
    """提交日 YYYYMMDD（bank_timezone 换算）——wedap 通用状态回查 oriReqDate 供参（0020）。

    用银行时区而非 UTC：wedap/银行按其本地交易日登记原单，UTC 跨午夜会差一天查不到。

    已知边界（codex P2 已评估接受）：日期在受理时生成，若提交恰跨银行午夜、wedap 按
    接收日登记，会差一天——后果只是该单通用回查 not found → reconcile 不收敛交 G6
    人工（方向安全，不会错收口）。窗口为秒级且写交易集中白天，不为此做相邻日重试。
    """
    tz = ZoneInfo(request.app.state.settings.bank_timezone)
    return dt.datetime.now(tz).strftime("%Y%m%d")


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
    "UYI": 0,
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
    # 解析/非有限/整数位容量三重守卫收口至共享原语（app/core/amounts.py），
    # 此处只做 AmountGuardError → HTTP 400 报文翻译；正数性/scale 是本层语义，保留在下方。
    _guard_msg = {
        "unparseable": f"bad amount: {raw!r}",
        "non_finite": f"amount must be finite: {raw!r}",
        "over_capacity": f"amount exceeds {MAX_INTEGER_DIGITS} integer digits: {raw!r}",
    }
    try:
        value = parse_guarded_decimal(raw)
    except AmountGuardError as exc:
        raise HTTPException(
            400,
            detail={"code": "GW_400_VALIDATION", "message": _guard_msg[exc.reason]},
        ) from exc
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


def _collect_detail_amounts(
    items: list[Any],
    *,
    amount_field: str,
    detail_key: str,
    currency: str,
    strict: bool,
) -> list[Decimal] | None:
    """从明细列表求各项金额，做 per-currency 护栏（scale/positive/finite）。

    - Any item is not a dict or lacks amount_field:
      - strict=False -> return None (opt-out: caller skips the sum check). Reserved for a
        future endpoint that genuinely has a wedap auto-allocation contract; no production
        caller uses it (disbursement/repayment lenders are strict by default).
      - strict=True -> 400 "invalid {detail_key} item: each item requires {amount_field}".
        Used for both the main detail (lenders, main_strict=True default) and the fee detail
        (feeDeductions.feeAmount, contract-mandatory), so a missing field cannot bypass the
        sum equation (codex P1).
    - Amount parse failure -> 400 "invalid {amount_field} in {detail_key} item".
    """
    amounts: list[Decimal] = []
    for item in items:
        if not isinstance(item, dict) or amount_field not in item:
            if strict:
                raise HTTPException(
                    400,
                    detail={
                        "code": "GW_400_VALIDATION",
                        "message": (
                            f"invalid {detail_key} item: each item requires {amount_field}"
                        ),
                    },
                )
            return None
        try:
            amounts.append(parse_amount(str(item[amount_field]), currency))
        except HTTPException as exc:
            raise HTTPException(
                400,
                detail={
                    "code": "GW_400_VALIDATION",
                    "message": f"invalid {amount_field} in {detail_key} item",
                },
            ) from exc
    return amounts


def validate_detail_consistency(
    body: dict[str, Any],
    *,
    total: Decimal | None,
    currency: str,
    detail_key: str,
    amount_field: str,
    fee_detail_key: str | None = None,
    fee_amount_field: str | None = None,
    main_strict: bool = True,
    require_detail: bool = False,
) -> None:
    """明细列表一致性前置校验（契约 C 透传原则：gateway 只校验不剪裁）。

    - detail_key absent from body, or its value is None: default require_detail=False -> skip
      (non-mandatory detail case); require_detail=True -> 400 "missing {detail_key}". Used by
      endpoints whose detail list is @NotEmpty in wedap (e.g. disbursement/repayment lenders):
      an entirely missing list is also invalid, so the gateway self-validates instead of
      relying on the wedap backstop (codex R1 finding fix).
    - Empty list -> 400 GW_400_VALIDATION "empty {detail_key}"
    - Any item has currencyCode != the top-level currency -> 400 "detail currency mismatch"
    - An item lacks amount_field (when a top-level total is present): default
      **main_strict=True -> 400** "invalid {detail_key} item: each item requires {amount_field}".
      The gateway self-validates the sum rather than relying on the downstream backstop
      (R4.5b guard-bypass fix: a lender missing txnAmount used to silently skip the sum check).
      Only when an endpoint genuinely has a wedap auto-allocation contract does the caller pass
      main_strict=False to keep the skip opt-out.
    - total=None → 无独立顶层总额（如 distribute 金额即取自明细 Σ），整段 sum 校验跳过，
      只保留「非空 + 币种一致」两项；避免对「明细自身求和再与自身比」的同义重复护栏

    **含费还款口径（fee_detail_key 提供时，如 feeDeductions）**：还款总额语义为「含费」——
    权威 wedap 契约 :169『从借款方扣除本金、利息、罚息、费用』+ 上游 admin-backend
    ``_align_amounts_for_baffle`` 硬绑 ``Σlender.txnAmount + Σfee.feeAmount == total``。
    故 fee_detail_key 提供时，sum 校验为 **Σ(主明细 amount_field) + Σ(费用明细 fee_amount_field)
    == total**；费用明细缺失/空则退化为纯主明细校验（纯本息还款口径不变）。**费用明细走严格
    模式**：非空时每项必须是 dict 且含合法 fee_amount_field（契约必填、无自动分配语义），
    A missing field / non-dict item -> 400; a missing field must not bypass the equation
    (codex P1). The main detail is strict by default too (main_strict=True: a missing
    amount_field / non-dict item -> 400, see above); only an explicit main_strict=False falls
    back to the "skip sum on missing field" opt-out, which no production caller uses
    (disbursement/repayment are strict by default plus require_detail).
    容器类型：detail_key / fee_detail_key 的值若非 list（extra=allow 可透传标量）→ 400。
    """
    if detail_key not in body or body[detail_key] is None:
        if require_detail:
            raise HTTPException(
                400,
                detail={"code": "GW_400_VALIDATION", "message": f"missing {detail_key}"},
            )
        return

    items = body[detail_key]
    if not isinstance(items, list):
        raise HTTPException(
            400,
            detail={"code": "GW_400_VALIDATION", "message": f"{detail_key} must be a list"},
        )
    if not items:
        raise HTTPException(
            400,
            detail={
                "code": "GW_400_VALIDATION",
                "message": f"empty {detail_key}",
            },
        )

    # 费用明细（可选第二来源，如 feeDeductions）——key + 金额字段名齐备且非 None 才纳入；
    # 值必须是 list（extra=allow 可透传标量 → 显式 400，避免 [*fee_items] 展开抛 500，codex P1）
    fee_items: list[Any] = []
    if fee_detail_key and fee_amount_field and body.get(fee_detail_key) is not None:
        raw_fee = body[fee_detail_key]
        if not isinstance(raw_fee, list):
            raise HTTPException(
                400,
                detail={
                    "code": "GW_400_VALIDATION",
                    "message": f"{fee_detail_key} must be a list",
                },
            )
        fee_items = raw_fee

    # currency mismatch 校验（主明细 + 费用明细，先于 sum，尽早拦截）
    for item in [*items, *fee_items]:
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

    # 费用明细 strict=True 校验**先于**主明细逃生口：非法费用项（非 dict / 缺 feeAmount）
    # 总是 400，不依赖主明细完整性——否则 lenders 缺字段走 strict=False 逃生口早退时，
    # 会连带跳过费用严格校验（codex R2 early-exit）。fee_items 非空 ⇒ 上方守卫已确保
    # fee_detail_key / fee_amount_field 均非 None。
    fee_amounts: list[Decimal] = []
    if fee_items and fee_amount_field and fee_detail_key:
        collected = _collect_detail_amounts(
            fee_items,
            amount_field=fee_amount_field,
            detail_key=fee_detail_key,
            currency=currency,
            strict=True,
        )
        # strict=True 下缺字段/非 dict 已 raise，非空 fee_items 必得非空 list（不会是 None）
        fee_amounts = collected or []

    # Main-detail sum: main_strict=True by default -> a missing field is a 400 (the gateway
    # self-validates rather than relying on the wedap backstop; fixes the R4.5b guard bypass).
    # Only an explicit main_strict=False falls back to the "skip sum on missing field" opt-out
    # (reserved for a future endpoint with a genuine wedap auto-allocation contract; opt-in).
    main_amounts = _collect_detail_amounts(
        items,
        amount_field=amount_field,
        detail_key=detail_key,
        currency=currency,
        strict=main_strict,
    )
    if main_amounts is None:
        return

    if sum(main_amounts) + sum(fee_amounts, Decimal("0")) != total:
        raise HTTPException(
            400,
            detail={
                "code": "GW_400_VALIDATION",
                "message": "detail amount sum mismatch",
            },
        )
