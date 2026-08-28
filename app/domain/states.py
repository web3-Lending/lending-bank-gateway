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
# v0.6.1 §4.2「业务错误码」）。
#
# **白名单制**：只有下列「确证拒绝」码才判 FAILED（终态、可安全回滚 + 换新 bizSeqNo 重发），
# 其余一律判 RESULT_UNKNOWN 挂起、等兜底 worker 用状态查询收敛真实结果。
# 为什么不用黑名单（只把 211/212 挑出来、其余默认 FAILED）：wedap 若新增业务码而未同步
# 通知，未知码会被默认判成「零资金变动可回滚」——但该码真实语义可能是「已扣款待处理」，
# 上游据此回滚即资金错账。两类误判代价不对称：
#   未知码误判 FAILED → 上游回滚而 wedap 已扣款 = 资金错账（不可逆）
#   未知码误判挂起     → 多等一轮 worker 查真实状态后收敛 = 延迟（可恢复）
# 故取代价小的一侧，与 debtSettled 只认真 boolean 的保守取值同一逻辑。
#
# 下列 10 码在文档中均明确为「受理即拒、零资金变动」：勾稽不平 201/202/216、币种 203、
# 账户信息不完整 204、余额不足 205、客户级互斥 207、借据级防重 208、缺 loanNo 209、
# 过渡户未配置 215。（206 账户信息暂不可用是 HTTP 500，不走本分支，由 5xx → RESULT_UNKNOWN
# 兜底；211 结果待确认 / 212 需人工处理明确要求转轮询，故不在本白名单内。）
WEDAP_TERMINAL_REJECT_CODES: frozenset[str] = frozenset(
    {
        "6605B00900201",
        "6605B00900202",
        "6605B00900203",
        "6605B00900204",
        "6605B00900205",
        "6605B00900207",
        "6605B00900208",
        "6605B00900209",
        "6605B00900215",
        "6605B00900216",
    }
)


# 通用交易（放款/归集/分发/退款/冲正）的「零资金变动」证据白名单——**按 HTTP 状态，不按
# 业务码**（2026-08-28 独立复核 BLOCKER-1 定案）。
#
# 为什么通用侧不能像还款那样列业务码：还款的 13 位码表在对接文档 v0.6.1 §4.2 有在册枚举，
# 通用侧**没有任何在册码表**。没有码表就无法区分「余额不足（分文未扣）」与「已扣款待人工」，
# 而 `WedapClient._unwrap` 在 HTTP 200 + 顶层 code 缺失时会抛 `WedapError(code="None")`
# （envelope 漂移实测可复现）——旧口径「通用分支恒 True」会把这种解析不出的响应断言成
# 「确认未产生影响，请换新 bizSeqNo 重发」= 重复放款。
#
# 唯一可信的结构化证据是 **wedap 在 HTTP 层把请求挡在门口**：这些状态下请求根本没进业务
# 引擎。逐值在册（而非「4xx 一律算」）：新出现的 4xx 默认不算证据，与白名单制同一逻辑。
# 明确排除：408（请求超时，可能已部分到达）、409（冲突，可能已存在同键交易）、
# 423/425/429（锁定/过早/限流，语义不保证未执行）。
WEDAP_DOOR_REJECT_HTTP_STATUSES: frozenset[int] = frozenset({400, 401, 403, 404, 405, 415, 422})


def is_door_reject_http_status(http_status: int | None) -> bool:
    """该 HTTP 状态是否构成「请求被门口拒绝、零资金变动」的证据。

    ``None``（无 HTTP 层证据，如 2xx 响应体里的业务码）与白名单外一律 False →
    调用方退 UNKNOWN 去查单，绝不假定零资金变动。
    """
    return http_status in WEDAP_DOOR_REJECT_HTTP_STATUSES


def is_repayment_terminal_reject(code: str) -> bool:
    """还款业务码是否为「确证拒绝」终态（零资金变动，可安全回滚）。

    白名单外一律 False → 调用方判 RESULT_UNKNOWN 挂起，绝不对未知码假定零资金变动。
    """
    return code in WEDAP_TERMINAL_REJECT_CODES


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
