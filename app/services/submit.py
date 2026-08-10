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
from app.domain.states import (
    WEDAP_PENDING_BUSINESS_CODES,
    OrderStatus,
    assert_transition,
    map_wedap_repayment_status,
    map_wedap_txn_status,
)
from app.models.txn import BankTxnOrder
from app.services.audit import write_audit
from app.services.idempotency import (
    IdempotencyConflict,
    IdempotencyInFlight,
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


def _parse_ack(
    data: dict[str, Any],
    *,
    repayment: bool,
) -> tuple[OrderStatus | None, dict[str, Any]]:
    """wedap 受理响应 → (终态映射 | None 非终态, 北向响应字段)。

    **北向字段名 `txnStatus` 恒定不变**：上游 lending-lifecycel 是只读仓、按 `txnStatus`
    解析（`bank_p2p.py`），故 gateway 在网关内部把 wedap 的契约变更消化掉——还款的
    `status` 归一化映射进 `txnStatus`，新增字段（debtSettled/globalTxId/detailStatus）
    以**增量**形式追加，上游零改动即恢复正确行为、按需再接新字段。

    非 str 状态（显式 null / 数字等毒响应）一律归一化为 `PROCESSING`：response_model 挂上后
    非 str 会触发 ResponseValidationError 500，且毒响应已冻结进 first_response → 同 key
    重放永久 500（独立评审 MEDIUM）。同时保守挂起优于误判失败回滚。
    """
    if not repayment:
        raw = data.get("txnStatus")
        return (
            map_wedap_txn_status(str(raw or "").upper()),
            {"txnStatus": raw if isinstance(raw, str) else "PROCESSING"},
        )

    # 还款（DTC 组合交易引擎，对接文档 v0.6.1 §4.2）
    raw_status = data.get("status")
    status = raw_status if isinstance(raw_status, str) else "PROCESSING"
    fields: dict[str, Any] = {
        "txnStatus": status,
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
    return map_wedap_repayment_status(status), fields


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
    IntegrityError（order 存在但幂等行缺失）→ IdempotencyConflict（上抛由 API 层转 409）。
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
                raise IdempotencyConflict(req.biz_seq_no) from None
    except IdempotencyInFlight:
        return {"txnStatus": "PROCESSING", "bizSeqNo": req.biz_seq_no, "inFlight": True}
    return None


async def submit_order(
    factory: async_sessionmaker[AsyncSession],
    *,
    wedap_call: Callable[..., Awaitable[dict[str, Any]]],
    req: SubmitRequest,
) -> dict[str, Any]:
    """受理：事务1 幂等+order(ACCEPTED) 落库（禁外呼）→ wedap 外呼 → 事务2 状态推进+回写。

    幂等三态：已完成→重放 first_response；in-flight（含崩溃重放）→ PROCESSING 响应零外呼；
    全新→执行。IdempotencyConflict 上抛由 API 层转 409。

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
        mapped, ack_fields = _parse_ack(data, repayment=req.repayment_contract)
        new_status = mapped or OrderStatus.SUBMITTED
        response: dict[str, Any] = {**ack_fields, "bizSeqNo": req.biz_seq_no}
    except (httpx.TimeoutException, httpx.TransportError):
        new_status = OrderStatus.RESULT_UNKNOWN
        response = {"txnStatus": "RESULT_UNKNOWN", "bizSeqNo": req.biz_seq_no}
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        if status_code >= 500:
            # 5xx：上游不可用，结果未知，保持可收敛幂等状态
            new_status = OrderStatus.RESULT_UNKNOWN
            response = {"txnStatus": "RESULT_UNKNOWN", "bizSeqNo": req.biz_seq_no}
        else:
            # 4xx：请求被上游拒绝，未执行，视为失败
            new_status = OrderStatus.FAILED
            response = {
                "txnStatus": "FAILED",
                "bizSeqNo": req.biz_seq_no,
                "errorCode": f"HTTP_{status_code}",
            }
    except WedapError as exc:
        # 待轮询业务码（还款 6605U00900211 结果待确认 / 6605B00900212 需人工处理）不是失败：
        # 此时 wedap 侧资金可能已部分变动或在柜面人工处置中，判 FAILED 会让上游回滚 →
        # 与 wedap 挂起态脱节（§3.6.1 状态撕裂）。改判 RESULT_UNKNOWN 挂起等兜底 worker 收敛。
        pending = exc.code in WEDAP_PENDING_BUSINESS_CODES
        new_status = OrderStatus.RESULT_UNKNOWN if pending else OrderStatus.FAILED
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
            if order.status == OrderStatus.ACCEPTED:
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
            # 幂等重放 data 段与首次逐字节一致（envelope trace_id 每请求各异）。
            response["orderStatus"] = str(order.status)
            await record_response(
                session,
                tenant_id=req.tenant_id,
                business_scope=req.business_scope,
                idempotency_key=req.biz_seq_no,
                response=response,
                final_effect_id=f"order:{req.biz_seq_no}",
            )
    return response
