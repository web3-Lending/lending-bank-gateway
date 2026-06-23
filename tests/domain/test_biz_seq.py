import pytest

from app.domain.biz_seq import validate_biz_seq_no

# biz_seq_no 格式以 lifecycel _gen_biz_seq_no 为准（hub 单一定义，ADR-0029 已 Superseded）；
# gateway 仅做存储安全 sanity，不再重编码格式。测试覆盖：信任 hub 的 WB 真码 + 旧格式向后兼容
# + 注入/控制字符防护。


@pytest.mark.parametrize(
    "v",
    [
        "WB1719000000000DISB1000010a3f2",  # lifecycel WB 真码（含小写随机尾）
        "WB1719000000000COLL1000020bcd1",  # 归集 COLL
        "DSB-20260611-0001234567890",  # 旧 ADR 格式仍接受（向后兼容，松校验不挑格式）
        "X" * 32,  # 32 字符边界
    ],
)
def test_valid_loose(v) -> None:
    """信任 hub：合法字符集 + ≤32 字符即通过（不强制具体格式）。"""
    validate_biz_seq_no(v)


@pytest.mark.parametrize(
    "v",
    [
        "",  # 空
        "X" * 33,  # 超 32 字符
        "WB1719 DISB100",  # 含空格（控制/注入字符）
        "biz;DROP",  # 分号注入
        "DSB/../etc",  # 路径符 . /
        "biz\nseq",  # 换行控制字符（中间）
        "biz\n",  # 尾随换行（fullmatch 防 Python `$` 漏过）
        "biz\t",  # 制表符控制字符
    ],
)
def test_invalid_loose(v) -> None:
    """空 / 超长 / 非安全字符集 → 拒绝。"""
    with pytest.raises(ValueError):
        validate_biz_seq_no(v)
