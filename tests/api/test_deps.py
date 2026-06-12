"""validate_detail_consistency 单元测试。"""

from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.api.deps import validate_detail_consistency


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
