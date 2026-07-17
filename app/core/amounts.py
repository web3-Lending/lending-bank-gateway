"""金额 Decimal 共享守卫原语（2026-07-17 三仓金额审计 · 批次 A 三重守卫统一收口）。

三重守卫 = ① 解析失败（InvalidOperation/ValueError/TypeError）
          ② 非有限（NaN/sNaN/Infinity——Decimal 合法构造但金融语义非法，
             NaN 会让 `<=` 比较抛 InvalidOperation 穿透成 500，且落对账比较后判等恒 false）
          ③ 整数位容量（超出目标金额列整数容量，落库炸 500；零值豁免——0E+100 的
             adjusted() 巨大但数值为零，可合法落库）

容量判定先按列 scale 半进位量化再看 adjusted()（与 MySQL DECIMAL 入库舍入一致）：
原值 99999999999999999.99995 的 adjusted()=16 看似在容量内，4dp 入库进位成 1E+17 才溢出，
只查原值会漏掉这类边界（codex 复核 P1）。

各层错误形态不同（HTTP 400 / DataQualityError / 跳过快照），本模块只做「解析+判定」
并抛统一 AmountGuardError，翻译成各层错误由调用方负责。正数性/精度拒收属调用方语义，不在此
（scale 超限在此不拒——按入库舍入语义参与容量判定，拒不拒超精度由 parse_amount 等调用方定）。
容量上限默认 17、列 scale 默认 4（本仓金额列统一 Numeric(21,4)）；其它列由调用点传参。
"""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# 本仓金额列统一 Numeric(21,4)：整数位容量 = 21 - 4 = 17，小数位 4
MAX_INTEGER_DIGITS = 17
COLUMN_SCALE = 4


class AmountGuardError(ValueError):
    """金额守卫违规。reason ∈ {"unparseable", "non_finite", "over_capacity"}。"""

    def __init__(self, reason: str, raw: object) -> None:
        self.reason = reason
        self.raw = raw
        super().__init__(f"{reason}: {raw!r}")


def parse_guarded_decimal(
    raw: object,
    *,
    max_integer_digits: int = MAX_INTEGER_DIGITS,
    column_scale: int = COLUMN_SCALE,
) -> Decimal:
    """str(raw)→Decimal 并施加三重守卫；违规抛 AmountGuardError。返回原值（不量化）。"""
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise AmountGuardError("unparseable", raw) from exc
    if not value.is_finite():
        raise AmountGuardError("non_finite", raw)
    # 按目标列 scale 半进位量化后查容量（与 MySQL DECIMAL 舍入一致），防进位边界漏判
    quantized = value.quantize(Decimal(1).scaleb(-column_scale), rounding=ROUND_HALF_UP)
    if not quantized.is_zero() and quantized.adjusted() >= max_integer_digits:
        raise AmountGuardError("over_capacity", raw)
    return value
