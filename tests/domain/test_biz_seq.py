import pytest

from app.domain.biz_seq import validate_biz_seq_no


@pytest.mark.parametrize("v", ["DSB-20260611-0001234567890", "RPY-20260611-9999999999"])
def test_valid(v) -> None:
    validate_biz_seq_no(v)


@pytest.mark.parametrize(
    "v",
    [
        "",
        "WB-1704067200000-DISB-10-0001-123456",
        "dsb-20260611-1",
        "DSB-2026-0001234567890",
        "X" * 33,
    ],
)
def test_invalid(v) -> None:
    with pytest.raises(ValueError):
        validate_biz_seq_no(v)


def test_prefix_too_short() -> None:
    """单字母前缀不合法（要求 2-4 位大写字母）。"""
    with pytest.raises(ValueError):
        validate_biz_seq_no("D-20260611-0001234567")


def test_prefix_too_long() -> None:
    """5 位前缀不合法（要求最多 4 位）。"""
    with pytest.raises(ValueError):
        validate_biz_seq_no("DSBXX-20260611-0001234567")


def test_seq_too_short() -> None:
    """末段不足 10 位数字不合法。"""
    with pytest.raises(ValueError):
        validate_biz_seq_no("DSB-20260611-123456789")


def test_max_length_boundary() -> None:
    """恰好 32 字符的合法 biz_seq_no 应通过。"""
    # 前缀 3 + '-' + 8位日期 + '-' + 数字段 = 3+1+8+1+19 = 32
    v = "DSB-20260611-" + "1" * 19
    assert len(v) == 32
    validate_biz_seq_no(v)


def test_over_max_length() -> None:
    """33 字符超出 32 上限应拒绝。"""
    v = "DSB-20260611-" + "1" * 20
    assert len(v) == 33
    with pytest.raises(ValueError):
        validate_biz_seq_no(v)


def test_lowercase_prefix_invalid() -> None:
    """小写前缀不合法。"""
    with pytest.raises(ValueError):
        validate_biz_seq_no("dsb-20260611-0001234567")
