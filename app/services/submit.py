import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.wedap import WedapError
from app.domain.biz_seq import validate_biz_seq_no
from app.domain.money_write import money_write_fields
from app.domain.states import (
    OrderStatus,
    assert_transition,
    is_door_reject_http_status,
    is_repayment_terminal_reject,
    map_wedap_repayment_status,
    map_wedap_txn_status,
)
from app.models.txn import BankTxnOrder
from app.services.audit import write_audit
from app.services.idempotency import (
    IdempotencyInFlight,
    IdempotencyKeyStateConflict,
    check_or_register,
    record_response,
)
from app.services.order_finalize import finalize_terminal_in_session, is_terminal

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubmitRequest:
    tenant_id: str
    biz_seq_no: str
    business_action: str
    biz_type: str
    amount: Decimal
    currency: str
    caller_service: str
    request_id: str
    business_scope: str
    wedap_payload: dict[str, Any]
    # 提交日 YYYYMMDD（bank_timezone，API 层换算注入）：wedap 通用状态回查 oriReqDate 供参。
    ori_req_date: str | None = None
    # 还款走 DTC 组合交易引擎的专属受理响应契约（对接文档 v0.6.1 §4.2，v0.5.0 起）：
    # 状态字段名 `status`（非 `txnStatus`），另有 detailStatus / debtSettled / globalTxId。
    # 仅 p2p-repayments 置 True；其余资金交易（放款/归集/分发/退款/冲正）仍走通用 txnStatus。
    repayment_contract: bool = False


# 北向 txnStatus 的封闭值域。落在集合外（含空串、未知值、异常大小写残留）一律输出
# RESULT_UNKNOWN——见 _parse_ack docstring 的状态撕裂说明。
# 通用交易（放款/归集/分发/退款/冲正）：对接文档 §5.5 交易状态枚举。
_GENERIC_NORTHBOUND_STATUSES = frozenset({"SUCCESS", "PROCESSING", "FAILED", "REVERSED", "PENDING"})
# 还款（DTC）：§4.2 收敛为三值，永不出现 REVERSED / PENDING。
_REPAYMENT_NORTHBOUND_STATUSES = frozenset({"SUCCESS", "PROCESSING", "FAILED"})


def _parse_ack(
    data: dict[str, Any],
    *,
    repayment: bool,
) -> tuple[OrderStatus | None, dict[str, Any], bool]:
    """wedap 受理响应 → (终态映射 | None 非终态, 北向响应字段, ack 可信否)。

    **北向字段名 `txnStatus` 恒定不变**：上游 lending-lifecycel 是只读仓、按 `txnStatus`
    解析（`bank_p2p.py`），故 gateway 在网关内部把 wedap 的契约变更消化掉——还款的
    `status` 归一化映射进 `txnStatus`，新增字段（debtSettled/globalTxId/detailStatus）
    以**增量**形式追加，上游零改动即恢复正确行为、按需再接新字段。

    **北向 txnStatus 值域封闭且归一化**（2026-08-10 独立评审 finding）：绝不原样透传 wedap
    的字符串。上游按字面量匹配（`_TXN_SUCCESS_STATUSES={"SUCCESS"}`），wedap 一旦返
    `"success"` 这类大小写偏差，gateway 台账会因内部 `.upper()` 正确落 SUCCEEDED，而北向
    原样输出的 `"success"` 上游认不出 → 走回滚分支 = **台账说成功、上游在回滚**的状态撕裂。
    未知值 / 毒值同理不可放行，一律归一化到 `RESULT_UNKNOWN`——上游只对该字面量挂起且不
    回滚（`app_flow_loans.py:1118`），是唯一安全的兜底档；`PROCESSING` 在上游反而触发回滚，
    故不能拿它当兜底值。

    **第三个返回值 ack_trusted**（2026-08-28 独立复核 MAJOR-2）：ack 状态是否落在封闭值域
    内。落不进（毒值 / 缺失 / 契约漂移）时台账仍保守落 SUBMITTED，但**不得**据此对外声称
    `outcome=ACCEPTED`（v2.2 §8.2 的定义是「直接上游已确认受理」）——网关在同一个响应里
    已经说了「我不信这个状态」（北向 RESULT_UNKNOWN），两句话不能自相矛盾。
    """
    if not repayment:
        raw = data.get("txnStatus")
        status = raw.strip().upper() if isinstance(raw, str) else ""
        trusted = status in _GENERIC_NORTHBOUND_STATUSES
        return (
            map_wedap_txn_status(status),
            {"txnStatus": status if trusted else "RESULT_UNKNOWN"},
            trusted,
        )

    # 还款（DTC 组合交易引擎，对接文档 v0.6.1 §4.2）
    raw_status = data.get("status")
    status = raw_status.strip().upper() if isinstance(raw_status, str) else ""
    trusted = status in _REPAYMENT_NORTHBOUND_STATUSES
    fields: dict[str, Any] = {
        # 还款值域仅 SUCCESS/PROCESSING/FAILED（§4.2 明确永不出现 REVERSED/PENDING）
        "txnStatus": status if trusted else "RESULT_UNKNOWN",
        # 核销依据：**仅 JSON 真 boolean true 才认**。缺失 / "true" 字符串 / null 一律 False——
        # 金融安全取保守方向：宁可不核销（可人工补），不可误核销（债务凭空消失）。
        "debtSettled": data.get("debtSettled") is True,
    }
    # 排查/对账锚点：globalTxId 是 wedap 组合交易实例号（报障时提供给 wedap），
    # detailStatus 供细分排查（状态机仍以 status 为准）。仅在 wedap 真给了字符串时透传。
    for key in ("globalTxId", "detailStatus"):
        value = data.get(key)
        if isinstance(value, str):
            fields[key] = value
    return map_wedap_repayment_status(status), fields, trusted


async def register_and_accept_order(
    factory: async_sessionmaker[AsyncSession],
    *,
    req: SubmitRequest,
) -> dict[str, Any] | None:
    """事务1：check_or_register 幂等登记 + 落 BankTxnOrder(ACCEPTED)（禁外呼），同事务 commit。

    返回：
      - dict → 直接作为响应 return（已完成重放的 first_response，或 in-flight 的
        PROCESSING 响应），调用方不外呼
      - None → 全新受理，order 已落 ACCEPTED，调用方继续外呼 + tx2
    IntegrityError（order 存在但幂等行缺失）→ IdempotencyKeyStateConflict（API 层转 409）。
    """
    try:
        async with factory() as session:
            try:
                async with session.begin():
                    hit = await check_or_register(
                        session,
                        tenant_id=req.tenant_id,
                        business_scope=req.business_scope,
                        idempotency_key=req.biz_seq_no,
                        method="POST",
                        path=req.business_scope,
                        payload=req.wedap_payload,
                    )
                    if hit is not None:
                        return hit
                    session.add(
                        BankTxnOrder(
                            tenant_id=req.tenant_id,
                            biz_seq_no=req.biz_seq_no,
                            business_action=req.business_action,
                            biz_type=req.biz_type,
                            amount=req.amount,
                            currency=req.currency,
                            caller_service=req.caller_service,
                            status=OrderStatus.ACCEPTED,
                            request_id=req.request_id,
                            trans_type=(str(req.wedap_payload.get("transType") or "") or None),
                            ori_req_date=req.ori_req_date,
                        )
                    )
            except IntegrityError:
                logger.error(
                    "order exists without idempotency record: %s/%s",
                    req.tenant_id,
                    req.biz_seq_no,
                )
                raise IdempotencyKeyStateConflict(req.biz_seq_no) from None
    except IdempotencyInFlight:
        # v2.2 §9.1「同键同 payload、已建立 durable operation 且仍未终态」：本仓的
        # durable operation（幂等行 + order 行）在 dispatch 前就已原子提交，故此处给得出
        # operationId/statusUrl。outcome=PENDING 而非 ACCEPTED——本路径零查询、不读 order
        # 行，**不知道上游是否已受理**，不能声称「上游已确认」。resubmitAllowed=False
        # （PENDING 强制），retryPolicy=POLL_STATUS 与既有 inFlight=true「去查单」信号同义。
        return {
            "txnStatus": "PROCESSING",
            "bizSeqNo": req.biz_seq_no,
            "inFlight": True,
            **money_write_fields(
                OrderStatus.ACCEPTED,
                no_effect_evidence=False,
                biz_seq_no=req.biz_seq_no,
                repayment=req.repayment_contract,
            ),
        }
    return None


async def submit_order(
    factory: async_sessionmaker[AsyncSession],
    *,
    wedap_call: Callable[..., Awaitable[dict[str, Any]]],
    req: SubmitRequest,
) -> dict[str, Any]:
    """受理：事务1 幂等+order(ACCEPTED) 落库（禁外呼）→ wedap 外呼 → 事务2 状态推进+回写。

    幂等三态：已完成→重放 first_response；in-flight（含崩溃重放）→ PROCESSING 响应零外呼；
    全新→执行。幂等拒绝（IdempotencyRejection 两子类）上抛由 API 层按各自 http_status 转
    422（payload 不符，v2.2 §9.1）/ 409（幂等键状态冲突）。

    外呼成功但事务2失败 → order 滞留 ACCEPTED：v1 运营收敛 SOP=对照 wedap 状态查询人工/worker
    推进（见 spec §7 在途单宽限）。
    """
    validate_biz_seq_no(req.biz_seq_no)
    early = await register_and_accept_order(factory, req=req)
    if early is not None:
        return early

    # 外呼（事务外）：成功→SUBMITTED；超时/传输错误→RESULT_UNKNOWN；
    # HTTPStatusError 5xx→RESULT_UNKNOWN（上游不可用结果未知）；
    # WedapError→FAILED（含 4xx 可解析 envelope——对接文档 v0.4.0/#82 起业务失败返
    # 422 + 业务码，client._unwrap 升格 WedapError，errorCode 保留 wedap 业务码）；
    # HTTPStatusError 4xx→FAILED（envelope 解析不出的兜底，errorCode=HTTP_4xx）
    try:
        data = await wedap_call(
            tenant_id=req.tenant_id,
            request_id=req.request_id,
            payload=req.wedap_payload,
        )
        # 同步优先：按 wedap HTTP 200 返回的状态映射 order 终态
        # SUCCESS（≤5s 同步终态）→ SUCCEEDED；FAILED（HTTP 200 业务失败）→ FAILED；
        # PROCESSING（>5s 异步在途）/ 缺省 / 未知 → SUBMITTED（保守，等回调/兜底 worker）
        # 通用交易读 txnStatus、还款读 status（DTC 新契约），归一化见 _parse_ack。
        mapped, ack_fields, ack_trusted = _parse_ack(data, repayment=req.repayment_contract)
        new_status = mapped or OrderStatus.SUBMITTED
        response: dict[str, Any] = {**ack_fields, "bizSeqNo": req.biz_seq_no}
        # v2.2 §8.2 NOT_APPLIED 的证据门（详见 app/domain/money_write 模块 docstring）：
        # **只有还款（DTC）契约的 FAILED 是零资金变动的权威证据**（§4.2「借款人分文未扣」，
        # 见 map_wedap_repayment_status docstring）。通用受理响应的 txnStatus=FAILED
        # 按本仓既有口径「仅表示终态」、不含零变动保证，故不作证据 → 退 UNKNOWN 去查单。
        no_effect_evidence = req.repayment_contract and mapped == OrderStatus.FAILED
    except (httpx.TimeoutException, httpx.TransportError):
        new_status = OrderStatus.RESULT_UNKNOWN
        response = {"txnStatus": "RESULT_UNKNOWN", "bizSeqNo": req.biz_seq_no}
        # 请求可能已到达 wedap（§9.3 资金结果不确定），零影响无从证明
        no_effect_evidence = False
        ack_trusted = True  # 无 ack 可言；台账落 RESULT_UNKNOWN，本标志不参与该行映射
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        ack_trusted = True
        if status_code >= 500:
            # 5xx：上游不可用，结果未知，保持可收敛幂等状态
            new_status = OrderStatus.RESULT_UNKNOWN
            response = {"txnStatus": "RESULT_UNKNOWN", "bizSeqNo": req.biz_seq_no}
            no_effect_evidence = False
        else:
            # 4xx：请求被上游拒绝，未执行，视为失败
            new_status = OrderStatus.FAILED
            response = {
                "txnStatus": "FAILED",
                "bizSeqNo": req.biz_seq_no,
                "errorCode": f"HTTP_{status_code}",
            }
            # 通用交易：**在册**的门口拒绝状态（未进业务引擎）才是零影响证据；408/409/429
            # 这类「可能已部分执行 / 可能已存在同键交易」的 4xx 不在册 → 退 UNKNOWN 去查单。
            # 还款除外：DTC 组合交易有分步执行态，一个解析不出的 4xx 不足以断言分文未扣，
            # 与 WEDAP_TERMINAL_REJECT_CODES 白名单外一律挂起同一保守口径。
            no_effect_evidence = not req.repayment_contract and is_door_reject_http_status(
                status_code
            )
    except WedapError as exc:
        # 还款走白名单制（对接文档 v0.6.1 §4.2 的 13 位业务码）：只有明确的「确证拒绝」码
        # 才是可回滚终态；待轮询码（211 结果待确认 / 212 需人工处理）与**任何未知码**一律
        # 挂 RESULT_UNKNOWN 等兜底 worker 查真实状态——此时 wedap 侧资金可能已部分变动或在
        # 柜面处置中，判 FAILED 会让上游回滚而与 wedap 挂起态脱节（§3.6.1 状态撕裂）。
        # 其余交易类型（放款/归集/分发/退款/冲正）保持既有语义：业务失败即终态 FAILED。
        ack_trusted = True
        terminal_reject = is_repayment_terminal_reject(exc.code) if req.repayment_contract else True
        new_status = OrderStatus.FAILED if terminal_reject else OrderStatus.RESULT_UNKNOWN
        # **台账终态与「零影响证据」是两件事，不共用一个布尔**（2026-08-28 复核 BLOCKER-1）：
        # 还款侧白名单码的定义本就是「确证拒绝：终态、零资金变动、可安全回滚」，两者同源；
        # 通用侧没有在册码表，`_unwrap` 在 HTTP 200 + 顶层 code 缺失时抛的 code="None"
        # 也走这里 —— 旧口径「通用恒 True」会把 envelope 漂移断言成「确认未产生影响，
        # 请换新 bizSeqNo 重发」= 重复放款。故通用侧只认 HTTP 层的门口拒绝证据，
        # 2xx 响应体里的业务码（含 code="None"）一律不算，与「通用 ack txnStatus=FAILED
        # 不作证据」保持同一强度（同样是 wedap 用 200 说业务失败，证据强度不能相反）。
        no_effect_evidence = (
            terminal_reject
            if req.repayment_contract
            else is_door_reject_http_status(exc.http_status)
        )
        response = {
            "txnStatus": str(new_status),
            "bizSeqNo": req.biz_seq_no,
            # 业务码保留：RESULT_UNKNOWN 也要能追溯到具体挂起原因（待确认 vs 待人工）。
            "errorCode": exc.code,
            # 业务失败文案（如「可用余额不足」「子账户不存在」）截断落幂等记录，
            # 供上游展示/排障；长度上限防异常上游把 first_response 撑爆。
            "errorMsg": exc.msg[:200],
        }

    # 事务2：CAS 状态推进（FOR UPDATE 读 order，仅当仍 ACCEPTED 才推进——防回调/兜底已
    # 聚合到更强终态被本次外呼结果盲写倒退，codex HIGH-1）+ 同步终态收口 + record_response。
    assert_transition(OrderStatus.ACCEPTED, new_status)
    now = dt.datetime.now(dt.UTC)
    async with factory() as session:
        async with session.begin():
            order = (
                await session.execute(
                    select(BankTxnOrder)
                    .where(
                        BankTxnOrder.tenant_id == req.tenant_id,
                        BankTxnOrder.biz_seq_no == req.biz_seq_no,
                    )
                    .with_for_update()
                )
            ).scalar_one()
            cas_applied = order.status == OrderStatus.ACCEPTED
            if cas_applied:
                order.status = new_status
                order.submitted_at = now
                if is_terminal(new_status):
                    # 同步终态：finalized_at/via + audit + 转发 lifecycle（稳定 key）
                    await finalize_terminal_in_session(
                        session,
                        order=order,
                        source="SYNC",
                        trace_id=req.request_id,
                        caller_service=req.caller_service,
                    )
                else:
                    # 非终态（SUBMITTED/RESULT_UNKNOWN）：仅审计，不转发（等回调/兜底）
                    await write_audit(
                        session,
                        tenant_id=req.tenant_id,
                        actor=f"svc:{req.caller_service}",
                        action=f"ORDER_{new_status}",
                        entity=f"bank_txn_order:{req.biz_seq_no}",
                        payload={
                            "business_action": req.business_action,
                            "amount": str(req.amount),
                        },
                    )
            # else：order 已被回调/兜底 worker 推进到更强态 → CAS skip 不覆盖，仅写 record_response
            # 提交响应最小字段契约：orderStatus = CAS 后订单真实状态（CAS skip 时即回调/兜底
            # 已聚合的更强态）。写在 record_response 前 → 随 first_response 一并冻结，
            # 幂等重放 data 段与首次**字段值**一致（不是字节一致：envelope trace_id 每请求
            # 各异，且 DB JSON 列不保证对象键顺序——字节序不是契约，字段值才是）。
            response["orderStatus"] = str(order.status)
            # v2.2 §8.2 MONEY_WRITE typed 字段（批次 2 纯增量：只加字段，HTTP 状态码不动）。
            # 以 **CAS 后的台账状态**为准而非本次 ack：回调/兜底已把单推到更强终态时
            # （CAS skip），typed 字段必须跟台账走。同样写在 record_response 前 →
            # 随 first_response 冻结，幂等重放与首次逐字段一致。
            response.update(
                money_write_fields(
                    OrderStatus(order.status),
                    no_effect_evidence=no_effect_evidence,
                    biz_seq_no=req.biz_seq_no,
                    repayment=req.repayment_contract,
                    # CAS skip 时台账状态来自回调/兜底 worker（可信来源），与本次毒值 ack
                    # 无关，不降级；只有本次 ack 真被写进台账时才受 ack 可信度约束。
                    ack_trusted=ack_trusted or not cas_applied,
                )
            )
            await record_response(
                session,
                tenant_id=req.tenant_id,
                business_scope=req.business_scope,
                idempotency_key=req.biz_seq_no,
                response=response,
                final_effect_id=f"order:{req.biz_seq_no}",
            )
    return response
