"""通用冲正编排：RVSL 台账单 SUCCEEDED + 原单同步翻 REVERSED（Public.md §4.4.2 同步无回调）。

与 submit_order 平行但语义不同：wedap 通用冲正 HTTP 200 即冲正指令受理成功（RVSL 单
SUCCEEDED），响应 txnStatus=REVERSED 描述的是**原单**新态——故在同一事务内把本地原单
SUCCEEDED→REVERSED（复用 finalize_terminal_in_session 升级路径，幂等）。幂等/记账等硬核
逻辑复用共享原语；两者若改幂等语义需同步维护。
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from sqlalchemy import select

from app.clients.wedap import WedapError
from app.domain.biz_seq import validate_biz_seq_no
from app.domain.states import IllegalTransition, OrderStatus, assert_transition
from app.models.txn import BankTxnOrder
from app.services.audit import write_audit
from app.services.idempotency import record_response
from app.services.order_finalize import finalize_terminal_in_session, is_terminal
from app.services.submit import SubmitRequest, register_and_accept_order

logger = logging.getLogger(__name__)


async def _reverse_original(
    session: Any,
    *,
    tenant_id: str,
    ori_biz_seq_no: str,
    trace_id: str,
    caller_service: str,
) -> None:
    """本地原单同步翻 REVERSED（复用升级 helper，幂等）。查不到不拦；已 REVERSED 跳过；
    非法转移（原单 FAILED/CANCELLED/EXPIRED，与 wedap 权威 REVERSED 分歧）记警告不翻、不炸请求。"""
    ori = (
        await session.execute(
            select(BankTxnOrder)
            .where(BankTxnOrder.tenant_id == tenant_id, BankTxnOrder.biz_seq_no == ori_biz_seq_no)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if ori is None:
        return  # 查不到不拦（原单可能非本 gateway 出，同 refund 口径）
    if OrderStatus(ori.status) == OrderStatus.REVERSED:
        return  # 幂等：已冲正
    try:
        assert_transition(OrderStatus(ori.status), OrderStatus.REVERSED)
    except IllegalTransition:
        logger.warning(
            "original %s/%s in %s cannot flip to REVERSED (wedap authoritative, skip local flip)",
            tenant_id,
            ori_biz_seq_no,
            ori.status,
        )
        return
    upgrade_from = str(ori.status)
    ori.status = OrderStatus.REVERSED
    await finalize_terminal_in_session(
        session,
        order=ori,
        source="SYNC",
        trace_id=trace_id,
        caller_service=caller_service,
        upgrade_from=upgrade_from,
    )


async def submit_reversal(
    factory: Any,
    *,
    wedap_reverse: Callable[..., Awaitable[dict[str, Any]]],
    req: SubmitRequest,
    ori_biz_seq_no: str,
) -> dict[str, Any]:
    """受理：事务1 幂等+RVSL(ACCEPTED) 落库（禁外呼）→ wedap 冲正外呼 → 事务2 RVSL 推进+
    原单翻转。"""
    validate_biz_seq_no(req.biz_seq_no)

    # 事务1：复用共享 helper（幂等登记 + 落 ACCEPTED 单）；命中重放/in-flight → 直接 return
    early = await register_and_accept_order(factory, req=req)
    if early is not None:
        return early

    # 外呼：HTTP 200 → 冲正指令受理成功（RVSL SUCCEEDED）；超时/5xx→RESULT_UNKNOWN；
    # 4xx/WedapError→FAILED
    try:
        data = await wedap_reverse(
            tenant_id=req.tenant_id, request_id=req.request_id, payload=req.wedap_payload
        )
        new_status = OrderStatus.SUCCEEDED
        response: dict[str, Any] = {
            "txnStatus": data.get("txnStatus", "REVERSED"),
            "bizSeqNo": req.biz_seq_no,
        }
    except (httpx.TimeoutException, httpx.TransportError):
        new_status = OrderStatus.RESULT_UNKNOWN
        response = {"txnStatus": "RESULT_UNKNOWN", "bizSeqNo": req.biz_seq_no}
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code >= 500:
            new_status = OrderStatus.RESULT_UNKNOWN
            response = {"txnStatus": "RESULT_UNKNOWN", "bizSeqNo": req.biz_seq_no}
        else:
            new_status = OrderStatus.FAILED
            response = {
                "txnStatus": "FAILED",
                "bizSeqNo": req.biz_seq_no,
                "errorCode": f"HTTP_{exc.response.status_code}",
            }
    except WedapError as exc:
        new_status = OrderStatus.FAILED
        response = {
            "txnStatus": "FAILED",
            "bizSeqNo": req.biz_seq_no,
            "errorCode": exc.code,
            "errorMsg": exc.msg[:200],
        }

    # 事务2：CAS 推进 RVSL + 原单翻转 + record_response
    assert_transition(OrderStatus.ACCEPTED, new_status)
    now = dt.datetime.now(dt.UTC)
    async with factory() as session:
        async with session.begin():
            rvsl = (
                await session.execute(
                    select(BankTxnOrder)
                    .where(
                        BankTxnOrder.tenant_id == req.tenant_id,
                        BankTxnOrder.biz_seq_no == req.biz_seq_no,
                    )
                    .with_for_update()
                )
            ).scalar_one()
            if rvsl.status == OrderStatus.ACCEPTED:
                rvsl.status = new_status
                rvsl.submitted_at = now
                if is_terminal(new_status):
                    await finalize_terminal_in_session(
                        session,
                        order=rvsl,
                        source="SYNC",
                        trace_id=req.request_id,
                        caller_service=req.caller_service,
                    )
                else:
                    await write_audit(
                        session,
                        tenant_id=req.tenant_id,
                        actor=f"svc:{req.caller_service}",
                        action=f"ORDER_{new_status}",
                        entity=f"bank_txn_order:{req.biz_seq_no}",
                        payload={"business_action": req.business_action, "amount": str(req.amount)},
                    )
                # 原单翻转仅在冲正受理成功时执行
                if new_status == OrderStatus.SUCCEEDED:
                    await _reverse_original(
                        session,
                        tenant_id=req.tenant_id,
                        ori_biz_seq_no=ori_biz_seq_no,
                        trace_id=req.request_id,
                        caller_service=req.caller_service,
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
