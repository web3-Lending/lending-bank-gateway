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

    SUCCESS → SUCCEEDED；FAILED → FAILED；REVERSED → REVERSED（对接文档 v0.3.0 §3.6：
    组合交易中途失败由 counter 人工介入，冲正后回传 REVERSED——非终态单可直接被冲正，
    不必先经 SUCCEEDED）；PENDING/PROCESSING/缺省/未知 → None。
    submit 同步收口对 None 回落 SUBMITTED；G2 status-query 收敛对 None 视为非终态 no-op
    （§3.6：PROCESSING=已有资金变动结果未知，必须挂起轮询、禁当失败回滚）。
    单一来源：submit 同步收口与 G2 兜底复用本函数，避免映射逻辑分叉。
    """
    s = txn_status.upper()
    if s == "SUCCESS":
        return OrderStatus.SUCCEEDED
    if s == "FAILED":
        return OrderStatus.FAILED
    if s == "REVERSED":
        return OrderStatus.REVERSED
    return None


# 非终态一律允许 → REVERSED：§3.6 组合交易中途失败不自动冲正，由 counter 人工冲正后
# 状态查询/回调回传 REVERSED——挂在 SUBMITTED/PROCESSING/RESULT_UNKNOWN（乃至外呼成功但
# 事务2失败滞留的 ACCEPTED）的单都可能被直接冲正，不必先经 SUCCEEDED。
_ALLOWED: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.ACCEPTED: {
        OrderStatus.SUBMITTED,
        OrderStatus.RESULT_UNKNOWN,
        OrderStatus.SUCCEEDED,  # 同步优先：wedap ≤5s 返 SUCCESS → 直接终态（配 tx2 CAS 防倒退）
        OrderStatus.FAILED,
        OrderStatus.CANCELLED,
        OrderStatus.REVERSED,
    },
    OrderStatus.SUBMITTED: {
        OrderStatus.PROCESSING,
        OrderStatus.RESULT_UNKNOWN,
        OrderStatus.SUCCEEDED,
        OrderStatus.FAILED,
        OrderStatus.EXPIRED,
        OrderStatus.REVERSED,
    },
    OrderStatus.PROCESSING: {
        OrderStatus.RESULT_UNKNOWN,
        OrderStatus.SUCCEEDED,
        OrderStatus.FAILED,
        OrderStatus.EXPIRED,
        OrderStatus.REVERSED,
    },
    OrderStatus.RESULT_UNKNOWN: {
        OrderStatus.PROCESSING,
        OrderStatus.SUCCEEDED,
        OrderStatus.FAILED,
        OrderStatus.EXPIRED,
        OrderStatus.REVERSED,
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
