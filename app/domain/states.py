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


def map_wedap_txn_status(txn_status: str) -> "OrderStatus | None":
    """wedap txnStatus → order 终态映射；非终态/未知返回 None。

    SUCCESS → SUCCEEDED；FAILED → FAILED；PROCESSING/缺省/未知 → None。
    submit 同步收口对 None 回落 SUBMITTED；G2 status-query 收敛对 None 视为非终态 no-op。
    单一来源：submit 同步收口与 G2 兜底复用本函数，避免映射逻辑分叉。
    """
    s = txn_status.upper()
    if s == "SUCCESS":
        return OrderStatus.SUCCEEDED
    if s == "FAILED":
        return OrderStatus.FAILED
    return None


_ALLOWED: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.ACCEPTED: {
        OrderStatus.SUBMITTED,
        OrderStatus.RESULT_UNKNOWN,
        OrderStatus.SUCCEEDED,  # 同步优先：wedap ≤5s 返 SUCCESS → 直接终态（配 tx2 CAS 防倒退）
        OrderStatus.FAILED,
        OrderStatus.CANCELLED,
    },
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
