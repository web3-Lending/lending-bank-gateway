"""MONEY_WRITE typed 响应字段（API 规范 v2.2 §8.2）——**纯映射，不新增状态判定**。

本模块把网关既有的资金写状态（:class:`app.domain.states.OrderStatus`）翻译成 v2.2
的封闭词汇 ``outcome`` / ``operationStatus`` / ``retryPolicy`` / ``resubmitAllowed``，
外加成对的 ``operationId`` / ``statusUrl``。**语义早就在本仓（上游超时/5xx 判
RESULT_UNKNOWN 而非 FAILED），本次只是换成规范的词。** HTTP 状态码、既有字段语义、
状态机、Problem 信封一律不动（资金写 HTTP 语义翻转是后续批次的事）。

为什么值得单独一层：批次 4 要把「200 包装失败」翻成真实 4xx/5xx，消费方必须先能按
typed 字段分支，才不会在翻转当天靠 ``txnStatus`` 字面量硬扛。

## NOT_APPLIED 的证据门（本模块最重要的一条）

v2.2：``NOT_APPLIED`` 必须有 dispatch 前原子证据，即「确认未产生影响」。本仓两段证据
缺一不可：

1. **dispatch 前原子记录**：``register_and_accept_order`` 在**外呼之前**于同一事务提交
   幂等行 + ``bank_txn_order``（§9.1 要求的可查询 operation + durable recovery
   evidence）。故 operationId/statusUrl 在下列每条路径上都真实可查。
2. **上游门口拒绝的权威证据**：由调用方传入 ``no_effect_evidence``。取值口径见
   ``app/services/submit.py`` 各 except 分支——只有「wedap 在受理阶段确证零资金变动」
   才为 True，两侧各有一张在册白名单，**其余一律 False**：

   - 还款（DTC）：``WEDAP_TERMINAL_REJECT_CODES`` 的 10 个业务码（对接文档 v0.6.1 §4.2）；
   - 通用（放款/归集/分发/退款/冲正）：``WEDAP_DOOR_REJECT_HTTP_STATUSES`` 的 HTTP 状态
     ——通用侧**没有任何在册业务码表**，故只认「wedap 在 HTTP 层把请求挡在门口」这一条
     结构化证据。

   于是**超时 / 传输中断 / 5xx / 还款非白名单业务码 / 通用受理响应 ``txnStatus=FAILED``
   / 通用 2xx 响应体里的业务码（含 envelope 漂移导致的 ``code="None"``）/ 未在册的 4xx
   一律 False**。倒数第二条是本仓自己的口径：``app/domain/states.py`` 明写「通用表的
   FAILED 仅表示终态」，不含零资金变动保证，与还款 DTC 契约的 FAILED（已确证借款人分文
   未扣）不同档；同一笔交易，wedap 用 200 body 说失败与用 200 body 里的业务码说失败，
   证据强度必须一致（2026-08-28 独立复核 BLOCKER-1：旧实现对通用分支恒判 True，
   实测一条 ``{"data":{"txnStatus":"SUCCESS"}}`` 的漂移响应会被断言成 NOT_APPLIED）。

证据不足时一律退回 ``UNKNOWN``（去查单/对账），绝不 NOT_APPLIED——两类误判代价不对称：
误判 NOT_APPLIED 会让上游回滚或换新 key 重发（资金错账，不可逆），误判 UNKNOWN 只是
多一次查单（可恢复）。这与 ``WEDAP_TERMINAL_REJECT_CODES`` 白名单制同一逻辑。

## resubmitAllowed 恒为 False

v2.2：``PENDING/UNKNOWN/ACCEPTED`` 必须 False；``NOT_APPLIED`` 仅在「权威策略明确允许」
时才可 True。本仓**没有 9000 在册的业务码映射**，不存在这样的权威策略，故 fail-closed
恒 False。将来接入 9000 目录后才谈得上按码放开同键重试（``RETRY_SAME_KEY_AFTER``）。
"""

from enum import StrEnum
from urllib.parse import quote

from app.domain.states import OrderStatus


class MoneyWriteOutcome(StrEnum):
    """v2.2 §8.2 ``outcome`` 封闭枚举：同步成功之外的业务意图事实。"""

    NOT_APPLIED = "NOT_APPLIED"
    PENDING = "PENDING"
    UNKNOWN = "UNKNOWN"
    ACCEPTED = "ACCEPTED"


class MoneyWriteOperationStatus(StrEnum):
    """v2.2 §8.2 ``operationStatus`` 受控枚举；终态 SUCCEEDED/REJECTED 后不得回处理中。"""

    PENDING = "PENDING"
    RECONCILING = "RECONCILING"
    SUCCEEDED = "SUCCEEDED"
    REJECTED = "REJECTED"


class MoneyWriteRetryPolicy(StrEnum):
    """v2.2 §8.2 ``retryPolicy``：写客户端唯一可执行的重试策略词表。"""

    NEVER = "NEVER"
    RETRY_SAME_KEY_AFTER = "RETRY_SAME_KEY_AFTER"
    POLL_STATUS = "POLL_STATUS"
    REAUTH_AND_REPLAY = "REAUTH_AND_REPLAY"
    CORRECT_AND_NEW_INTENT = "CORRECT_AND_NEW_INTENT"


def operation_status_url(biz_seq_no: str, *, repayment: bool) -> str:
    """本次 operation 的查单地址（与 operationId 成对给出，v2.2 §8.2 强制）。

    两个查单端点都按请求头 ``X-Tenant-Id`` 过滤 ``bank_txn_order`` 且在 S2S 门内，
    满足「受同 tenant/principal 的对象级授权」。还款走专用端点：受理响应为 PROCESSING
    时按该端点轮询才拿得到 ``debtSettled`` / ``steps[]``（通用 5.5 查询不提供）。

    ``quote`` 不是装饰：``validate_biz_seq_no`` 当前只放行 ``[A-Za-z0-9_-]``，
    但那是**另一个模块的**不变量；此处不假设它永不放宽。
    """
    key = quote(biz_seq_no, safe="")
    if repayment:
        return f"/api/v1/loans/p2p-repayments/{key}/status"
    return f"/api/v1/bank-funds/status?bizSeqNo={key}"


def money_write_fields(
    order_status: OrderStatus,
    *,
    no_effect_evidence: bool,
    biz_seq_no: str,
    repayment: bool,
    ack_trusted: bool = True,
) -> dict[str, str | bool]:
    """order 台账状态 → v2.2 §8.2 typed 字段（合法组合表逐行对齐）。

    ``order_status`` 取 **CAS 之后的订单真实状态**（台账权威），不是本次外呼的 ack：
    回调/兜底 worker 已把单推到更强终态时，本次响应必须跟着台账走。

    | 台账状态 | outcome | operationStatus | retryPolicy |
    |---|---|---|---|
    | SUCCEEDED | 省略（由成功模型表达） | SUCCEEDED | NEVER |
    | ACCEPTED（本地已受理，上游未确认） | PENDING | PENDING | POLL_STATUS |
    | SUBMITTED / PROCESSING（上游已受理**且 ack 可信**） | ACCEPTED | PENDING | POLL_STATUS |
    | FAILED **且** 有零影响证据 | NOT_APPLIED | REJECTED | CORRECT_AND_NEW_INTENT |
    | 其余（RESULT_UNKNOWN / 无证据 FAILED / REVERSED / EXPIRED /
      CANCELLED …） | UNKNOWN | RECONCILING | POLL_STATUS |

    ``ACCEPTED`` 只表示直接上游已确认受理，**绝不表示已经入账或完成**（v2.2 原文）；
    北向文案沿用 ``txnStatus``，不把 accepted 展示成「已完成」。

    末行是保守兜底：``REVERSED`` / ``PARTIALLY_REVERSED`` 已经产生过资金影响再被撤回，
    ``NOT_APPLIED``（确认未产生影响）会是谎报；``EXPIRED`` / ``CANCELLED`` 同样拿不到
    「零变动」证据。四值枚举无法表达这些，故一律指向查单/对账。

    ``ack_trusted=False`` 是 SUBMITTED/PROCESSING 行的**降级开关**（2026-08-28 独立复核
    MAJOR-2）：毒值 / 缺失 / 契约漂移的受理响应会让台账保守落 SUBMITTED，但那不是「直接
    上游已确认受理」，只是「已 dispatch 但当前不能确认终态」→ 走末行 UNKNOWN/RECONCILING
    进对账（§9.3），不能对外声称 ACCEPTED。否则同一个响应里北向 ``txnStatus`` 说
    RESULT_UNKNOWN（我不信这个状态）、``outcome`` 却说 ACCEPTED（上游已确认受理），
    自相矛盾且会让消费方只轮询、不进对账。

    ``CORRECT_AND_NEW_INTENT`` 而非 ``RETRY_SAME_KEY_AFTER``：本仓的确证拒绝口径是
    「可安全回滚 + **换新 bizSeqNo** 重发」（``app/domain/states.py``），而 bizSeqNo
    就是幂等键——同键重发只会拿回同一条冻结的失败响应。
    """
    fields: dict[str, str]
    if order_status == OrderStatus.SUCCEEDED:
        # 同步完成：outcome 省略（v2.2 明确「由成功模型表达」，不填无意义默认值）
        fields = {
            "operationStatus": MoneyWriteOperationStatus.SUCCEEDED,
            "retryPolicy": MoneyWriteRetryPolicy.NEVER,
        }
    elif order_status == OrderStatus.ACCEPTED:
        fields = {
            "outcome": MoneyWriteOutcome.PENDING,
            "operationStatus": MoneyWriteOperationStatus.PENDING,
            "retryPolicy": MoneyWriteRetryPolicy.POLL_STATUS,
        }
    elif order_status in (OrderStatus.SUBMITTED, OrderStatus.PROCESSING) and ack_trusted:
        fields = {
            "outcome": MoneyWriteOutcome.ACCEPTED,
            "operationStatus": MoneyWriteOperationStatus.PENDING,
            "retryPolicy": MoneyWriteRetryPolicy.POLL_STATUS,
        }
    elif order_status == OrderStatus.FAILED and no_effect_evidence:
        fields = {
            "outcome": MoneyWriteOutcome.NOT_APPLIED,
            "operationStatus": MoneyWriteOperationStatus.REJECTED,
            "retryPolicy": MoneyWriteRetryPolicy.CORRECT_AND_NEW_INTENT,
        }
    else:
        fields = {
            "outcome": MoneyWriteOutcome.UNKNOWN,
            "operationStatus": MoneyWriteOperationStatus.RECONCILING,
            "retryPolicy": MoneyWriteRetryPolicy.POLL_STATUS,
        }
    return {
        **fields,
        # 无 9000 权威放行策略 → fail-closed（见模块 docstring）
        "resubmitAllowed": False,
        # durable operation 在 dispatch 前已原子落库，故 operationId/statusUrl 恒可成对给出
        "operationId": biz_seq_no,
        "statusUrl": operation_status_url(biz_seq_no, repayment=repayment),
    }


def money_write_reject_fields() -> dict[str, str | bool]:
    """MONEY_WRITE 写错误：网关在 dispatch 前拒绝（§8.2「有证据确认未产生影响」行）。

    适用于**请求根本没进 submit/reversal 服务**的 4xx：报文校验、必填缺失、金额护栏、
    幂等键与 bizSeqNo 不一致、wedap 拒收字段等。此时既没外呼、也没建 durable operation，
    「未产生影响」是网关自己的原子事实，不依赖上游任何断言——这是本仓唯一敢无条件给
    ``NOT_APPLIED`` 的场景。

    **不给 operationId/statusUrl**：v2.2 要求二者「有 durable operation 时成对出现」；
    这条路径压根没建单，给了就是死链（查单端点必然 404），比不给更糟。
    ``operationStatus`` 同理留空（规范原文「``REJECTED`` 或无 operation」）。
    """
    return {
        "outcome": MoneyWriteOutcome.NOT_APPLIED,
        "retryPolicy": MoneyWriteRetryPolicy.CORRECT_AND_NEW_INTENT,
        # 无 9000 权威放行策略 → fail-closed（同 money_write_fields）
        "resubmitAllowed": False,
    }


def money_write_unresolved_fields(biz_seq_no: str, *, repayment: bool) -> dict[str, str | bool]:
    """MONEY_WRITE 写错误：已建 durable operation、但本次调用无法确认其结果。

    两条路径：

    - **409 幂等冲突**（order 行在、幂等行缺失）：v2.2 §9.1 要求调用方「停止重放并先查询
      原 operation」——这正是 ``UNKNOWN`` + ``POLL_STATUS`` + 查单地址要表达的。冲突的
      前提就是 order 行存在，故 statusUrl 必然可查，不是死链。
    - **dispatch 之后抛出的 4xx**（如上游返 2xx 但响应体不是 JSON，解析异常经 ``ValueError``
      冒泡成 400）：外呼可能已经打出去了，绝不能声称零影响。

    两者都落 §8.2「已 dispatch 但当前不能确认终态」行，与超时/5xx 同档。
    """
    return {
        "outcome": MoneyWriteOutcome.UNKNOWN,
        "operationStatus": MoneyWriteOperationStatus.RECONCILING,
        "retryPolicy": MoneyWriteRetryPolicy.POLL_STATUS,
        "resubmitAllowed": False,
        "operationId": biz_seq_no,
        "statusUrl": operation_status_url(biz_seq_no, repayment=repayment),
    }
