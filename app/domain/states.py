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


# 还款（DTC 组合交易引擎）专属：受理阶段业务拒绝返 HTTP 200 + 13 位业务码（对接文档
# v0.6.1 §4.2「业务错误码」）。下列两码的真实语义是**转轮询**而非失败——
#   6605U00900211 交易结果待确认 → 「转入轮询，按专用状态查询接口跟进」
#   6605B00900212 交易需人工处理 → 「等待 WeDAP 柜面运营处置，持续轮询」
# 此时 wedap 侧资金可能已部分变动或正在人工处置中，若当 FAILED 收口，上游会据此回滚，
# 与 wedap 挂起态脱节（§3.6.1 状态撕裂）。故映射到 RESULT_UNKNOWN 挂起等兜底 worker 收敛。
# 其余业务码（勾稽不平 201/202/216、币种 203、账户 204、余额不足 205、互斥 207、
# 借据防重 208、缺 loanNo 209、过渡户未配 215）均为**受理即拒、零资金变动**，保持 FAILED。
WEDAP_PENDING_BUSINESS_CODES: frozenset[str] = frozenset(
    {
        "6605U00900211",
        "6605B00900212",
    }
)


def map_wedap_repayment_status(status: str) -> "OrderStatus | None":
    """还款受理响应 `status` → order 终态映射（对接文档 v0.6.1 §4.2）；非终态返回 None。

    与 map_wedap_txn_status 的差异（故不复用而并列）：
    - 字段名是 `status` 不是 `txnStatus`（v0.5.0 起 wedap 切 DTC 引擎后重写受理响应体）
    - 值域收敛为三值 SUCCESS / PROCESSING / FAILED，**永不出现 REVERSED / PENDING**
      （还款失败不自动冲正，柜面人工处置只前向推进）
    - FAILED 语义更强：wedap 已确证**零资金变动**（借款人分文未扣），可安全回滚 +
      换新 bizSeqNo 重发；而通用表的 FAILED 仅表示终态
    未知值（含空/毒值）→ None，由调用方回落非终态 SUBMITTED 挂起轮询——绝不当失败回滚。
    """
    s = status.upper()
    if s == "SUCCESS":
        return OrderStatus.SUCCEEDED
    if s == "FAILED":
        return OrderStatus.FAILED
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


# 吸收态：除「SUCCEEDED→REVERSED / PARTIALLY_REVERSED→REVERSED」升级外不再接受任何迁移的
# 状态集合。与 TERMINAL_STATUSES（触发收口转发的业务终态）语义不同：CANCELLED/EXPIRED 不
# 转发但同样不可再迁移——两谓词共用曾致 CANCELLED/EXPIRED+REVERSED 回调走到 assert_transition
# 抛 IllegalTransition → inbox 永留 RECEIVED 无限重放（codex P2，2026-07-15）。
ABSORBING_STATUSES: frozenset[OrderStatus] = frozenset(
    {
        OrderStatus.SUCCEEDED,
        OrderStatus.FAILED,
        OrderStatus.REVERSED,
        OrderStatus.CANCELLED,
        OrderStatus.EXPIRED,
    }
)


class IllegalTransition(Exception):
    pass


def assert_transition(src: OrderStatus, dst: OrderStatus) -> None:
    if dst not in _ALLOWED[src]:
        raise IllegalTransition(f"{src} -> {dst}")
