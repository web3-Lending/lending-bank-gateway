from decimal import Decimal
from enum import StrEnum


class OrderStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    SUBMITTED = "SUBMITTED"
    PROCESSING = "PROCESSING"
    RESULT_UNKNOWN = "RESULT_UNKNOWN"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    PARTIALLY_REVERSED = "PARTIALLY_REVERSED"
    REVERSED = "REVERSED"


class LegStatus(StrEnum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    REVERSED = "REVERSED"
    REVERSAL = "REVERSAL"


_ALLOWED: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.ACCEPTED: {OrderStatus.SUBMITTED, OrderStatus.FAILED, OrderStatus.CANCELLED},
    OrderStatus.SUBMITTED: {
        OrderStatus.PROCESSING,
        OrderStatus.RESULT_UNKNOWN,
        OrderStatus.SUCCEEDED,
        OrderStatus.FAILED,
        OrderStatus.EXPIRED,
    },
    OrderStatus.PROCESSING: {
        OrderStatus.RESULT_UNKNOWN,
        OrderStatus.SUCCEEDED,
        OrderStatus.FAILED,
        OrderStatus.EXPIRED,
    },
    OrderStatus.RESULT_UNKNOWN: {
        OrderStatus.PROCESSING,
        OrderStatus.SUCCEEDED,
        OrderStatus.FAILED,
        OrderStatus.EXPIRED,
    },
    OrderStatus.SUCCEEDED: {OrderStatus.PARTIALLY_REVERSED, OrderStatus.REVERSED},
    OrderStatus.PARTIALLY_REVERSED: {OrderStatus.REVERSED},
    OrderStatus.FAILED: set(),
    OrderStatus.EXPIRED: set(),
    OrderStatus.CANCELLED: set(),
    OrderStatus.REVERSED: set(),
}


class IllegalTransition(Exception):
    pass


def assert_transition(src: OrderStatus, dst: OrderStatus) -> None:
    if dst not in _ALLOWED[src]:
        raise IllegalTransition(f"{src} -> {dst}")


def aggregate_order_status(legs: list[tuple[LegStatus, str]]) -> OrderStatus:
    """spec §6 父子聚合表。legs: [(status, amount_str)]；REVERSAL 金额按覆盖度判部分/全额冲正。

    SUBMITTED 后 wedap 拆 leg 期间无 leg 数据 → 空列表视为在途（PROCESSING）。

    优先级（从高到低）：
    1. UNKNOWN → RESULT_UNKNOWN
    2. PENDING → PROCESSING
    3. FAILED 优先于冲正判定（saga 补偿后整单仍为 FAILED）
    4. REVERSAL → REVERSED / PARTIALLY_REVERSED
    5. 全 SUCCESS → SUCCEEDED
    """
    if not legs:
        return OrderStatus.PROCESSING
    statuses = [s for s, _ in legs]
    if LegStatus.UNKNOWN in statuses:
        return OrderStatus.RESULT_UNKNOWN
    if LegStatus.PENDING in statuses:
        return OrderStatus.PROCESSING
    # FAILED 优先于冲正判定：saga 补偿场景（FAILED leg + REVERSAL leg 共存）
    # 整单业务语义仍为 FAILED，补偿性 REVERSAL 不改变失败结论。
    if LegStatus.FAILED in statuses:
        return OrderStatus.FAILED
    # REVERSED leg 必须有对应 REVERSAL leg；否则数据畸形
    if any(s == LegStatus.REVERSED for s in statuses) and not any(
        s == LegStatus.REVERSAL for s in statuses
    ):
        raise ValueError("malformed legs: REVERSED without REVERSAL")
    reversal = sum(Decimal(a) for s, a in legs if s == LegStatus.REVERSAL)
    if reversal > 0:
        gross = sum(Decimal(a) for s, a in legs if s in (LegStatus.SUCCESS, LegStatus.REVERSED))
        if gross == 0:
            raise ValueError("malformed legs: REVERSAL without source leg")
        return OrderStatus.REVERSED if reversal >= gross else OrderStatus.PARTIALLY_REVERSED
    return OrderStatus.SUCCEEDED
