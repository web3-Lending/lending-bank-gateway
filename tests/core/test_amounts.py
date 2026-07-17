"""app/core/amounts.py 三重守卫原语测试。"""

from decimal import Decimal

import pytest

from app.core.amounts import MAX_INTEGER_DIGITS, AmountGuardError, parse_guarded_decimal


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("abc", "unparseable"),
        (None, "unparseable"),
        (object(), "unparseable"),
        ("NaN", "non_finite"),
        ("sNaN", "non_finite"),
        ("Infinity", "non_finite"),
        ("-Infinity", "non_finite"),
        # 1e17：adjusted()=17 == MAX_INTEGER_DIGITS，超 Numeric(21,4) 整数容量
        ("100000000000000000", "over_capacity"),
        ("1E+17", "over_capacity"),
        ("-1E+17", "over_capacity"),
    ],
)
def test_parse_guarded_decimal_rejects(raw: object, reason: str) -> None:
    with pytest.raises(AmountGuardError) as exc_info:
        parse_guarded_decimal(raw)
    assert exc_info.value.reason == reason
    assert exc_info.value.raw is raw


@pytest.mark.parametrize(
    "raw",
    # "0E+100"/"-0E+50"：adjusted() 巨大但值为零 → 必须放行（零值豁免）
    ["0.01", "100.0000", "99999999999999999.9999", 42, Decimal("7.5"), "-3.2", "0E+100", "-0E+50"],
)
def test_parse_guarded_decimal_accepts(raw: object) -> None:
    value = parse_guarded_decimal(raw)
    assert isinstance(value, Decimal)
    assert value.is_finite()
    assert value.is_zero() or value.adjusted() < MAX_INTEGER_DIGITS


def test_parse_guarded_decimal_capacity_overridable() -> None:
    """max_integer_digits 可由调用点收紧（recon Numeric(24,8) 列复用场景）。"""
    with pytest.raises(AmountGuardError) as exc_info:
        parse_guarded_decimal("1E+16", max_integer_digits=16)
    assert exc_info.value.reason == "over_capacity"
    assert parse_guarded_decimal("1E+16").adjusted() == 16  # 默认 17 位下放行
