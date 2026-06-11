import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.wedap import WedapError
from app.domain.biz_seq import validate_biz_seq_no
from app.domain.states import OrderStatus, assert_transition
from app.models.txn import BankTxnOrder
from app.services.idempotency import (
    IdempotencyConflict,
    IdempotencyInFlight,
    check_or_register,
    record_response,
)

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

    # 外呼（事务外）：成功→SUBMITTED；超时/传输错误→RESULT_UNKNOWN；WedapError→FAILED
    try:
        data = await wedap_call(
            tenant_id=req.tenant_id,
            request_id=req.request_id,
            payload=req.wedap_payload,
        )
        new_status = OrderStatus.SUBMITTED
        response: dict[str, Any] = {
            "txnStatus": data.get("txnStatus", "PROCESSING"),
            "bizSeqNo": req.biz_seq_no,
        }
    except (httpx.TimeoutException, httpx.TransportError):
        new_status = OrderStatus.RESULT_UNKNOWN
        response = {"txnStatus": "RESULT_UNKNOWN", "bizSeqNo": req.biz_seq_no}
    except WedapError as exc:
        new_status = OrderStatus.FAILED
        response = {"txnStatus": "FAILED", "bizSeqNo": req.biz_seq_no, "errorCode": exc.code}

    # 事务2：assert_transition 守卫非法直写 + update order 状态 + submitted_at + record_response
    assert_transition(OrderStatus.ACCEPTED, new_status)
    now = dt.datetime.now(dt.UTC)
    async with factory() as session:
        async with session.begin():
            await session.execute(
                update(BankTxnOrder)
                .where(
                    BankTxnOrder.tenant_id == req.tenant_id,
                    BankTxnOrder.biz_seq_no == req.biz_seq_no,
                )
                .values(status=new_status, submitted_at=now)
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
