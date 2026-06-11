import re

_PATTERN = re.compile(r"^[A-Z]{2,4}-\d{8}-\d{10,}$")  # ADR-0029


def validate_biz_seq_no(value: str) -> None:
    if len(value) > 32 or not _PATTERN.match(value):
        raise ValueError(f"biz_seq_no 不符合 ADR-0029 格式: {value!r}")
