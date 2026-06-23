import re

# biz_seq_no 的格式/生成算法以 lifecycel `_gen_biz_seq_no` 为单一定义源（hub 生成、下游透传；
# ADR-0029 已 Superseded）。gateway 作为消费方**不再重编码格式规则**（那正是历史漂移的根源），
# 仅做「存储安全 sanity」：非空 + ≤32 字符 + 安全字符集（字母数字与 - _），拦截注入/控制字符。
_MAX_LEN = 32
# fullmatch 而非 match：Python 正则 `$` 在尾随换行前也匹配，match 会放过 "biz\n"；
# fullmatch 要求整串被消费，尾随换行/控制字符不漏。
_SAFE = re.compile(r"[A-Za-z0-9_-]+")


def validate_biz_seq_no(value: str) -> None:
    if not value or len(value) > _MAX_LEN or not _SAFE.fullmatch(value):
        raise ValueError(f"biz_seq_no 非法（空 / 超 {_MAX_LEN} 字符 / 含非法字符）: {value!r}")
