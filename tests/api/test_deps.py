"""validate_detail_consistency 单元测试。"""

from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.api.deps import parse_amount, validate_detail_consistency

# ── parse_amount: 脏金额护栏（QA-M finding：NaN/Infinity 不得穿透成 500）──────────


@pytest.mark.parametrize("raw", ["NaN", "sNaN", "Infinity", "-Infinity", "inf", "nan"])
def test_parse_amount_rejects_non_finite_400(raw: str) -> None:
    """NaN/sNaN/Infinity 是合法 Decimal 但金额非法 → 必须 400，而非穿透成 500。

    回归 QA-M：旧实现下 NaN 会让 `value <= 0` 抛 InvalidOperation（未捕获 → 500），
    Infinity 通过 `<= 0` 被当正数放行，最终在 Numeric(21,4) 落库炸 500。
    """
    with pytest.raises(HTTPException) as exc_info:
        parse_amount(raw)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["code"] == "GW_400_VALIDATION"  # type: ignore[index]


def test_parse_amount_accepts_positive_finite() -> None:
    """正有限值正常返回 Decimal。"""
    assert parse_amount("100.0000") == Decimal("100.0000")


@pytest.mark.parametrize("raw", ["0", "-1", "-0.0001"])
def test_parse_amount_rejects_non_positive_400(raw: str) -> None:
    """0 与负数 → 400（既有正数护栏，确保未被新分支回归）。"""
    with pytest.raises(HTTPException) as exc_info:
        parse_amount(raw)
    assert exc_info.value.status_code == 400


def test_parse_amount_rejects_unparseable_400() -> None:
    """无法解析为 Decimal → 400 bad amount。"""
    with pytest.raises(HTTPException) as exc_info:
        parse_amount("not-a-number")
    assert exc_info.value.status_code == 400


@pytest.mark.parametrize("raw", ["100000000000000000", "1E+17"])
def test_parse_amount_rejects_over_capacity_400(raw: str) -> None:
    """整数位 ≥17 超出 Numeric(21,4) 整数容量 → 400，而非落库炸 500。"""
    with pytest.raises(HTTPException) as exc_info:
        parse_amount(raw)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["code"] == "GW_400_VALIDATION"  # type: ignore[index]
    assert "integer digits" in exc_info.value.detail["message"]  # type: ignore[index]


def test_no_detail_key_passes() -> None:
    """明细键不在 body 中 → 跳过，不报错。"""
    validate_detail_consistency(
        {"bizSeqNo": "X"},
        total=Decimal("100.0000"),
        currency="USD",
        detail_key="lenders",
        amount_field="lendAmount",
    )


def test_empty_detail_list_raises_400() -> None:
    """明细键存在但列表为空 → 400 GW_400_VALIDATION。"""
    with pytest.raises(HTTPException) as exc_info:
        validate_detail_consistency(
            {"lenders": []},
            total=Decimal("100.0000"),
            currency="USD",
            detail_key="lenders",
            amount_field="lendAmount",
        )
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["code"] == "GW_400_VALIDATION"
    assert "empty lenders" in exc_info.value.detail["message"]


def test_sum_mismatch_raises_400() -> None:
    """明细金额合计 != 顶层 total → 400 GW_400_VALIDATION。"""
    with pytest.raises(HTTPException) as exc_info:
        validate_detail_consistency(
            {"lenders": [{"lendAmount": "60.0000"}, {"lendAmount": "30.0000"}]},
            total=Decimal("100.0000"),
            currency="USD",
            detail_key="lenders",
            amount_field="lendAmount",
        )
    assert exc_info.value.status_code == 400
    assert "sum mismatch" in exc_info.value.detail["message"]


def test_sum_match_passes() -> None:
    """明细金额合计 == 顶层 total → 放行。"""
    validate_detail_consistency(
        {"lenders": [{"lendAmount": "60.0000"}, {"lendAmount": "40.0000"}]},
        total=Decimal("100.0000"),
        currency="USD",
        detail_key="lenders",
        amount_field="lendAmount",
    )


def test_currency_mismatch_raises_400() -> None:
    """明细项 currencyCode != 顶层 currency → 400 GW_400_VALIDATION。"""
    with pytest.raises(HTTPException) as exc_info:
        validate_detail_consistency(
            {
                "lenders": [
                    {"lendAmount": "100.0000", "currencyCode": "HKD"},
                ]
            },
            total=Decimal("100.0000"),
            currency="USD",
            detail_key="lenders",
            amount_field="lendAmount",
        )
    assert exc_info.value.status_code == 400
    assert "currency mismatch" in exc_info.value.detail["message"]


def test_currency_match_passes() -> None:
    """明细项 currencyCode == 顶层 currency → 放行。"""
    validate_detail_consistency(
        {
            "lenders": [
                {"lendAmount": "100.0000", "currencyCode": "USD"},
            ]
        },
        total=Decimal("100.0000"),
        currency="USD",
        detail_key="lenders",
        amount_field="lendAmount",
    )


def test_no_amount_field_in_items_skips_sum_check() -> None:
    """明细项无 amount_field（wedap 自动分配场景）→ 跳过 sum 校验，放行。"""
    validate_detail_consistency(
        {"lenders": [{"userId": "u1"}, {"userId": "u2"}]},
        total=Decimal("100.0000"),
        currency="USD",
        detail_key="lenders",
        amount_field="lendAmount",
    )


def test_userlist_sum_mismatch_raises_400() -> None:
    """bank_funds userList amount sum 不匹配 → 400。"""
    with pytest.raises(HTTPException) as exc_info:
        validate_detail_consistency(
            {"userList": [{"amount": "50.0000"}, {"amount": "40.0000"}]},
            total=Decimal("100.0000"),
            currency="USD",
            detail_key="userList",
            amount_field="amount",
        )
    assert exc_info.value.status_code == 400
    assert "sum mismatch" in exc_info.value.detail["message"]


def test_userlist_empty_raises_400() -> None:
    """bank_funds userList 为空 → 400。"""
    with pytest.raises(HTTPException) as exc_info:
        validate_detail_consistency(
            {"userList": []},
            total=Decimal("100.0000"),
            currency="USD",
            detail_key="userList",
            amount_field="amount",
        )
    assert exc_info.value.status_code == 400
    assert "empty userList" in exc_info.value.detail["message"]


def test_non_dict_item_skipped_in_currency_check() -> None:
    """明细列表含非 dict 项（如字符串）→ currency 循环跳过，不报错，继续处理后续项。"""
    # 非 dict 项在 currency 循环中被 continue 跳过；后续 dict 项无 amount_field → 跳过 sum 校验
    validate_detail_consistency(
        {"lenders": ["not-a-dict", {"userId": "u1"}]},
        total=Decimal("100.0000"),
        currency="USD",
        detail_key="lenders",
        amount_field="lendAmount",
    )


def test_invalid_amount_field_value_raises_400() -> None:
    """明细项 amount_field 值无法解析为 Decimal → 400 GW_400_VALIDATION。"""
    with pytest.raises(HTTPException) as exc_info:
        validate_detail_consistency(
            {"lenders": [{"lendAmount": "not-a-number"}]},
            total=Decimal("100.0000"),
            currency="USD",
            detail_key="lenders",
            amount_field="lendAmount",
        )
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["code"] == "GW_400_VALIDATION"
    assert "invalid lendAmount" in exc_info.value.detail["message"]


def test_none_detail_value_passes() -> None:
    """detail_key 存在但值为 None → 等同缺省，跳过校验。
    修复前：body[detail_key] 取到 None，直接走 if not items → 400 empty。
    修复后：None 视同缺省，早期 return。
    """
    validate_detail_consistency(
        {"lenders": None},
        total=Decimal("100.0000"),
        currency="USD",
        detail_key="lenders",
        amount_field="lendAmount",
    )


def test_none_userlist_value_passes() -> None:
    """userList=None → 同样跳过校验（bank_funds 场景）。"""
    validate_detail_consistency(
        {"userList": None},
        total=Decimal("500.0000"),
        currency="USD",
        detail_key="userList",
        amount_field="amount",
    )


# ── G5: per-currency 精度护栏（v1 全局 ≤4dp scale，拒静默 round）──────────────


@pytest.mark.parametrize("raw", ["1.23000", "0.00001", "1.234567"])
def test_parse_amount_rejects_over_4dp_scale_400(raw: str) -> None:
    """G5：小数位 >4dp → 400 scale 超限（防 Numeric(21,4) 静默 round），含显式末尾零 1.23000。"""
    with pytest.raises(HTTPException) as exc_info:
        parse_amount(raw)
    assert exc_info.value.status_code == 400
    assert "scale" in exc_info.value.detail["message"]  # type: ignore[index]


@pytest.mark.parametrize("raw", ["1.2345", "100", "1E+3", "0.0001"])
def test_parse_amount_accepts_within_4dp_scale(raw: str) -> None:
    """G5：≤4dp（含指数表示 1E+3=1000）正常返回正数。"""
    assert parse_amount(raw) > 0


# ── per-currency 精度护栏（FU-GW-PER-CURRENCY-SCALE）─────────────────────────
# 带 currency 时按该币种 ISO-4217 小数位（USD/默认 2、JPY 0、BHD 3、CLF 4）校验
# 规范化有效位（去尾零），拒亚单位超精度脏金额；不传 currency 时退回 G5 全局 ≤4dp。


@pytest.mark.parametrize(
    ("raw", "currency"),
    [
        ("100.5", "JPY"),  # JPY 0dp，任何小数非法
        ("0.1", "JPY"),
        ("1.235", "USD"),  # USD 2dp，3 位有效非法（亚分）
        ("1.235", "EUR"),  # 默认 2dp（不在特例表）同理
        ("1.2345", "BHD"),  # BHD 3dp，4 位有效非法
        ("100.5", "jpy"),  # 大小写不敏感
    ],
)
def test_parse_amount_rejects_over_currency_scale_400(raw: str, currency: str) -> None:
    """带币种时超该币种精度 → 400，message 含 scale + 币种。"""
    with pytest.raises(HTTPException) as exc_info:
        parse_amount(raw, currency)
    assert exc_info.value.status_code == 400
    assert "scale" in exc_info.value.detail["message"]  # type: ignore[index]
    assert currency.upper() in exc_info.value.detail["message"]  # type: ignore[index]


@pytest.mark.parametrize(
    ("raw", "currency", "expected"),
    [
        ("100", "JPY", Decimal("100")),  # JPY 整数
        ("100.0000", "JPY", Decimal("100.0000")),  # 上游补尾零，规范化后 0 位有效，放行
        ("1.23", "USD", Decimal("1.23")),  # USD 2dp
        ("100.0000", "USD", Decimal("100.0000")),  # USD 补尾零放行（关键防回归）
        ("1.234", "BHD", Decimal("1.234")),  # BHD 3dp
        ("1.2300", "BHD", Decimal("1.2300")),  # BHD 尾零规范化 2 位有效，放行
        ("1.2345", "CLF", Decimal("1.2345")),  # CLF 4dp
    ],
)
def test_parse_amount_accepts_within_currency_scale(
    raw: str, currency: str, expected: Decimal
) -> None:
    """带币种且有效位 ≤ 该币种精度（含上游补尾零）正常返回原值。"""
    assert parse_amount(raw, currency) == expected


@pytest.mark.parametrize("currency", ["XYZ", "ZZZ"])
def test_parse_amount_unknown_currency_defaults_2dp(currency: str) -> None:
    """未识别币种按 ISO 默认 2dp 校验（gateway 仅法币，String(3)）。"""
    assert parse_amount("1.23", currency) == Decimal("1.23")
    with pytest.raises(HTTPException) as exc_info:
        parse_amount("1.235", currency)
    assert exc_info.value.status_code == 400


def test_parse_amount_no_currency_keeps_global_4dp() -> None:
    """不传 currency（如 distribute 内部聚合）退回 G5 全局原始 ≤4dp：尾零照算。"""
    assert parse_amount("1.2345") == Decimal("1.2345")
    with pytest.raises(HTTPException):
        parse_amount("1.23000")  # 原始 5 位（含尾零）仍按 G5 拒


def test_parse_amount_uyi_is_zero_dp() -> None:
    """ISO-4217 UYI（乌拉圭指数单位）为 0dp，须在 _CURRENCY_SCALE 内（codex finding 1）。"""
    assert parse_amount("100", "UYI") == Decimal("100")
    with pytest.raises(HTTPException) as exc_info:
        parse_amount("1.5", "UYI")
    assert exc_info.value.status_code == 400


# ── 明细金额也走 per-currency 护栏（codex finding 2：明细绕过 guard）──────────


def test_detail_amount_sub_currency_scale_rejected_400() -> None:
    """明细 lendAmount 亚单位超精度（USD 0.615）即使 sum 对得上也 400，不透传 Wedap。"""
    with pytest.raises(HTTPException) as exc_info:
        validate_detail_consistency(
            {"lenders": [{"lendAmount": "0.615"}, {"lendAmount": "0.615"}]},
            total=Decimal("1.23"),
            currency="USD",
            detail_key="lenders",
            amount_field="lendAmount",
        )
    assert exc_info.value.status_code == 400
    assert "invalid lendAmount" in exc_info.value.detail["message"]  # type: ignore[index]


def test_detail_amount_jpy_decimal_rejected_400() -> None:
    """JPY 0dp 明细带小数 → 400（明细金额也按币种精度校验）。"""
    with pytest.raises(HTTPException) as exc_info:
        validate_detail_consistency(
            {"lenders": [{"lendAmount": "60.5"}, {"lendAmount": "39.5"}]},
            total=Decimal("100"),
            currency="JPY",
            detail_key="lenders",
            amount_field="lendAmount",
        )
    assert exc_info.value.status_code == 400

# ── 含费还款三等式（fee_detail_key）：Σlender.txnAmount + Σfee.feeAmount == total ──
# 口径来源：权威 wedap 契约 :169 + 上游 admin-backend _align_amounts_for_baffle
# (FU-GW-REPAY-FEE-SUMGUARD-20260720-001)。


def test_fee_three_way_sum_ok() -> None:
    """含费还款：Σlender(2.80) + Σfee(0.20) == total(3.00) → 放行。"""
    validate_detail_consistency(
        {
            "lenders": [{"txnAmount": "2.8000"}],
            "feeDeductions": [{"feeType": "PENALTY", "feeAmount": "0.2000"}],
        },
        total=Decimal("3.0000"),
        currency="USD",
        detail_key="lenders",
        amount_field="txnAmount",
        fee_detail_key="feeDeductions",
        fee_amount_field="feeAmount",
    )


def test_fee_three_way_multi_fee_ok() -> None:
    """多费种：Σlender(90) + SERVICE(7) + OTHER(3) == total(100) → 放行。"""
    validate_detail_consistency(
        {
            "lenders": [{"txnAmount": "60.0000"}, {"txnAmount": "30.0000"}],
            "feeDeductions": [
                {"feeType": "SERVICE", "feeAmount": "7.0000"},
                {"feeType": "OTHER", "feeAmount": "3.0000"},
            ],
        },
        total=Decimal("100.0000"),
        currency="USD",
        detail_key="lenders",
        amount_field="txnAmount",
        fee_detail_key="feeDeductions",
        fee_amount_field="feeAmount",
    )


def test_fee_three_way_sum_mismatch_400() -> None:
    """含费不平：Σlender(2.80) + Σfee(0.20) = 3.00 != total(3.50) → 400。"""
    with pytest.raises(HTTPException) as exc_info:
        validate_detail_consistency(
            {
                "lenders": [{"txnAmount": "2.8000"}],
                "feeDeductions": [{"feeType": "PENALTY", "feeAmount": "0.2000"}],
            },
            total=Decimal("3.5000"),
            currency="USD",
            detail_key="lenders",
            amount_field="txnAmount",
            fee_detail_key="feeDeductions",
            fee_amount_field="feeAmount",
        )
    assert exc_info.value.status_code == 400
    assert "sum mismatch" in exc_info.value.detail["message"]


def test_fee_ignoring_fee_would_wrongly_400_regression() -> None:
    """回归：老口径(只核 Σlender==total) 会把含费还款误判 400；新口径纳入 fee 后放行。

    Σlender(2.80) != total(3.00) 在老逻辑必 400；带 feeDeductions(0.20) 后三等式成立。
    """
    validate_detail_consistency(
        {
            "lenders": [{"txnAmount": "2.8000"}],
            "feeDeductions": [{"feeType": "PENALTY", "feeAmount": "0.2000"}],
        },
        total=Decimal("3.0000"),
        currency="USD",
        detail_key="lenders",
        amount_field="txnAmount",
        fee_detail_key="feeDeductions",
        fee_amount_field="feeAmount",
    )


def test_fee_absent_degrades_to_lender_only() -> None:
    """无 feeDeductions：退化为纯本息 Σlender==total（纯本息还款口径不变）。"""
    validate_detail_consistency(
        {"lenders": [{"txnAmount": "100.0000"}]},
        total=Decimal("100.0000"),
        currency="USD",
        detail_key="lenders",
        amount_field="txnAmount",
        fee_detail_key="feeDeductions",
        fee_amount_field="feeAmount",
    )


def test_fee_empty_list_degrades_to_lender_only() -> None:
    """feeDeductions 为空列表：Σfee=0，退化为 Σlender==total。"""
    validate_detail_consistency(
        {"lenders": [{"txnAmount": "100.0000"}], "feeDeductions": []},
        total=Decimal("100.0000"),
        currency="USD",
        detail_key="lenders",
        amount_field="txnAmount",
        fee_detail_key="feeDeductions",
        fee_amount_field="feeAmount",
    )


def test_fee_item_missing_amount_field_skips_sum() -> None:
    """费用明细项缺 feeAmount（wedap 自动分配）→ 跳过 sum 校验，放行。"""
    validate_detail_consistency(
        {
            "lenders": [{"txnAmount": "2.8000"}],
            "feeDeductions": [{"feeType": "PENALTY"}],
        },
        total=Decimal("3.0000"),
        currency="USD",
        detail_key="lenders",
        amount_field="txnAmount",
        fee_detail_key="feeDeductions",
        fee_amount_field="feeAmount",
    )


def test_fee_item_invalid_amount_400() -> None:
    """费用明细金额非法（超精度）→ 400 invalid feeAmount in feeDeductions item。"""
    with pytest.raises(HTTPException) as exc_info:
        validate_detail_consistency(
            {
                "lenders": [{"txnAmount": "2.8000"}],
                "feeDeductions": [{"feeType": "PENALTY", "feeAmount": "0.20001"}],
            },
            total=Decimal("3.0000"),
            currency="USD",
            detail_key="lenders",
            amount_field="txnAmount",
            fee_detail_key="feeDeductions",
            fee_amount_field="feeAmount",
        )
    assert exc_info.value.status_code == 400
    assert "invalid feeAmount in feeDeductions item" in exc_info.value.detail["message"]
