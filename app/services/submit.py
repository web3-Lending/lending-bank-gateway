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
from app.domain.states import OrderStatus, assert_transition, map_wedap_txn_status
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

    # 事务1：check_or_register + BankTxnOrder(ACCEPTED) 落库，同事务 commit（禁外呼）
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
                        # 已完成重放：直接返回 first_response，零外呼
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
                            # 通用状态回查供参（0020）：transType 存调用方原值——wedap 按
                            # (oriBizSeqNo, transType) 消歧，查询值必须等于提交值。
                            # 无损落库（codex P1）：入口 pydantic 已强制必填且 ≤20，禁止
                            # 静默截断（截断致回查值 != 提交值，单据永久查不到）。
                            trans_type=(str(req.wedap_payload.get("transType") or "") or None),
                            ori_req_date=req.ori_req_date,
                        )
                    )
            except IntegrityError:
                # order 已存在但幂等行缺失（人工补数/迁移脏状态）
                logger.error(
                    "order exists without idempotency record: %s/%s",
                    req.tenant_id,
                    req.biz_seq_no,
                )
                raise IdempotencyConflict(req.biz_seq_no) from None
    except IdempotencyInFlight:
        # 崩溃重放/并发重放：事务1 已 commit 但 record_response 未写，禁止重复执行业务
        return {"txnStatus": "PROCESSING", "bizSeqNo": req.biz_seq_no, "inFlight": True}

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
        # 同步优先：按 wedap HTTP 200 返回的 txnStatus 映射 order 终态
        # SUCCESS（≤5s 同步终态）→ SUCCEEDED；FAILED（HTTP 200 业务失败）→ FAILED；
        # PROCESSING（>5s 异步在途）/ 缺省 / 未知 → SUBMITTED（保守，等回调/兜底 worker）
        wedap_status = str(data.get("txnStatus", "")).upper()
        # 同步收口复用 G2 共享映射；None（PROCESSING/缺省/未知）回落 SUBMITTED 保持既有语义。
        new_status = map_wedap_txn_status(wedap_status) or OrderStatus.SUBMITTED
        response: dict[str, Any] = {
            "txnStatus": data.get("txnStatus", "PROCESSING"),
            "bizSeqNo": req.biz_seq_no,
        }
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
        new_status = OrderStatus.FAILED
        response = {
            "txnStatus": "FAILED",
            "bizSeqNo": req.biz_seq_no,
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
            await record_response(
                session,
                tenant_id=req.tenant_id,
                business_scope=req.business_scope,
                idempotency_key=req.biz_seq_no,
                response=response,
                final_effect_id=f"order:{req.biz_seq_no}",
            )
    return response
