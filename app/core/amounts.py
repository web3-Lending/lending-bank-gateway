"""金额 Decimal 共享守卫原语（2026-07-17 三仓金额审计 · 批次 A 三重守卫统一收口）。

三重守卫 = ① 解析失败（InvalidOperation/ValueError/TypeError）
          ② 非有限（NaN/sNaN/Infinity——Decimal 合法构造但金融语义非法，
             NaN 会让 `<=` 比较抛 InvalidOperation 穿透成 500，且落对账比较后判等恒 false）
          ③ 整数位容量（adjusted() >= max_integer_digits 超出目标金额列整数容量，落库炸 500；
             零值豁免——0E+100 的 adjusted() 巨大但数值为零，可合法落库）

各层错误形态不同（HTTP 400 / DataQualityError / 跳过快照），本模块只做「解析+判定」
并抛统一 AmountGuardError，翻译成各层错误由调用方负责。正数性/scale 属调用方语义，不在此。
容量上限默认 17（本仓金额列统一 Numeric(21,4)）；其它列容量由调用点传 max_integer_digits。
"""

from decimal import Decimal, InvalidOperation

# 本仓金额列统一 Numeric(21,4)：整数位容量 = 21 - 4 = 17
MAX_INTEGER_DIGITS = 17


class AmountGuardError(ValueError):
    """金额守卫违规。reason ∈ {"unparseable", "non_finite", "over_capacity"}。"""

    def __init__(self, reason: str, raw: object) -> None:
        self.reason = reason
        self.raw = raw
        super().__init__(f"{reason}: {raw!r}")


def parse_guarded_decimal(raw: object, *, max_integer_digits: int = MAX_INTEGER_DIGITS) -> Decimal:
    """str(raw)→Decimal 并施加三重守卫；违规抛 AmountGuardError。"""
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise AmountGuardError("unparseable", raw) from exc
    if not value.is_finite():
        raise AmountGuardError("non_finite", raw)
    if not value.is_zero() and value.adjusted() >= max_integer_digits:
        raise AmountGuardError("over_capacity", raw)
    return value
