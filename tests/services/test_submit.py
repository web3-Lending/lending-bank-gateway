from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from app.core.db import build_engine, build_session_factory
from app.domain.states import OrderStatus
from app.models.audit import AuditLog
from app.models.base import Base
from app.models.txn import BankTxnOrder
from app.services.submit import SubmitRequest, submit_order


@pytest.fixture()
async def factory():
    engine = build_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield build_session_factory(engine)
    await engine.dispose()


def _req(**kw) -> SubmitRequest:
    d = dict(
        tenant_id="OCBC",
        biz_seq_no="DSB-20260611-0001234567890",
        business_action="DISBURSE",
        biz_type="DSB",
        amount=Decimal("100.0000"),
        currency="USD",
        caller_service="lifecycle",
        request_id="req-1",
        business_scope="p2p_disburse",
        wedap_payload={"bizSeqNo": "DSB-20260611-0001234567890"},
    )
    d.update(kw)
    return SubmitRequest(**d)


@pytest.mark.asyncio
async def test_accepted_flow_sets_submitted(factory) -> None:
    wedap = AsyncMock()
    wedap.submit_disbursement.return_value = {"txnStatus": "PROCESSING"}
    result = await submit_order(factory, wedap_call=wedap.submit_disbursement, req=_req())
    assert result["txnStatus"] == "PROCESSING"
    async with factory() as s:
        order = (await s.execute(select(BankTxnOrder))).scalar_one()
        assert order.status == OrderStatus.SUBMITTED and order.submitted_at is not None


@pytest.mark.asyncio
async def test_timeout_sets_result_unknown(factory) -> None:
    wedap = AsyncMock()
    wedap.submit_disbursement.side_effect = httpx.ConnectTimeout("t")
    result = await submit_order(factory, wedap_call=wedap.submit_disbursement, req=_req())
    assert result["txnStatus"] == "RESULT_UNKNOWN"
    async with factory() as s:
        assert (
            await s.execute(select(BankTxnOrder))
        ).scalar_one().status == OrderStatus.RESULT_UNKNOWN


@pytest.mark.asyncio
async def test_wedap_business_failure_sets_failed(factory) -> None:
    from app.clients.wedap import WedapError

    wedap = AsyncMock()
    wedap.submit_disbursement.side_effect = WedapError("422", "BUSINESS_RULE_VIOLATION")
    result = await submit_order(factory, wedap_call=wedap.submit_disbursement, req=_req())
    assert result["txnStatus"] == "FAILED" and result["errorCode"] == "422"


@pytest.mark.asyncio
async def test_idempotent_replay_returns_first_response_without_second_call(factory) -> None:
    wedap = AsyncMock()
    wedap.submit_disbursement.return_value = {"txnStatus": "PROCESSING"}
    await submit_order(factory, wedap_call=wedap.submit_disbursement, req=_req())
    again = await submit_order(factory, wedap_call=wedap.submit_disbursement, req=_req())
    assert again["txnStatus"] == "PROCESSING"
    assert wedap.submit_disbursement.await_count == 1


@pytest.mark.asyncio
async def test_inflight_replay_returns_processing_without_business_execution(factory) -> None:
    """崩溃重放：事务1 已 commit（幂等行+order 在库）但未 record_response → InFlight → PROCESSING，零外呼。"""  # noqa: E501
    wedap = AsyncMock()
    wedap.submit_disbursement.return_value = {"txnStatus": "PROCESSING"}
    # 用 monkeypatch/mock 让第一次 submit 在外呼后、事务2 前中断：直接手工构造状态——
    # 简单可靠做法：自己在 factory 里执行事务1 等价操作（check_or_register + order 落库 commit），
    # 然后调 submit_order 断言 inFlight 响应且 wedap 零调用、order 仍 ACCEPTED。
    from app.domain.states import OrderStatus as OS
    from app.services.idempotency import check_or_register

    req = _req()
    async with factory() as s:
        async with s.begin():
            await check_or_register(
                s,
                tenant_id=req.tenant_id,
                business_scope=req.business_scope,
                idempotency_key=req.biz_seq_no,
                method="POST",
                path=req.business_scope,
                payload=req.wedap_payload,
            )
            s.add(
                BankTxnOrder(
                    tenant_id=req.tenant_id,
                    biz_seq_no=req.biz_seq_no,
                    business_action=req.business_action,
                    biz_type=req.biz_type,
                    amount=req.amount,
                    currency=req.currency,
                    caller_service=req.caller_service,
                    status=OS.ACCEPTED,
                    request_id=req.request_id,
                )
            )
    result = await submit_order(factory, wedap_call=wedap.submit_disbursement, req=req)
    assert result.get("inFlight") is True and result["txnStatus"] == "PROCESSING"
    assert wedap.submit_disbursement.await_count == 0


@pytest.mark.asyncio
async def test_conflict_propagates(factory) -> None:
    from app.services.idempotency import IdempotencyConflict

    wedap = AsyncMock()
    wedap.submit_disbursement.return_value = {"txnStatus": "PROCESSING"}
    await submit_order(factory, wedap_call=wedap.submit_disbursement, req=_req())
    with pytest.raises(IdempotencyConflict):
        await submit_order(
            factory,
            wedap_call=wedap.submit_disbursement,
            req=_req(wedap_payload={"bizSeqNo": "DSB-20260611-0001234567890", "amount": "999"}),
        )


@pytest.mark.asyncio
async def test_invalid_biz_seq_no_rejected(factory) -> None:
    wedap = AsyncMock()
    with pytest.raises(ValueError):
        await submit_order(
            factory,
            wedap_call=wedap.submit_disbursement,
            req=_req(biz_seq_no="WB-1704067200000-DISB-10-0001-123456"),
        )


@pytest.mark.asyncio
async def test_order_exists_without_idempotency_raises_conflict(factory, caplog) -> None:
    """order 已存在但幂等行缺失（人工补数/迁移脏状态）→ IdempotencyConflict + error 日志。"""
    import logging

    from app.services.idempotency import IdempotencyConflict

    # 预插 order 但不插幂等行（模拟脏状态）
    req = _req()
    async with factory() as s:
        async with s.begin():
            s.add(
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

    wedap = AsyncMock()
    with caplog.at_level(logging.ERROR, logger="app.services.submit"):
        with pytest.raises(IdempotencyConflict):
            await submit_order(factory, wedap_call=wedap.submit_disbursement, req=req)

    assert any("order exists without idempotency record" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_submit_writes_audit_log(factory) -> None:
    """受理成功后 audit_log 有一行 action=ORDER_SUBMITTED。"""
    wedap = AsyncMock()
    wedap.submit_disbursement.return_value = {"txnStatus": "PROCESSING"}
    await submit_order(factory, wedap_call=wedap.submit_disbursement, req=_req())
    async with factory() as s:
        row = (await s.execute(select(AuditLog))).scalar_one()
        assert row.action == "ORDER_SUBMITTED"
        assert row.actor == "svc:lifecycle"
        assert row.entity == f"bank_txn_order:{_req().biz_seq_no}"


def _make_http_status_error(status_code: int) -> httpx.HTTPStatusError:
    """构造带 response 的 HTTPStatusError（mock httpx 场景）。"""
    response = httpx.Response(status_code=status_code)
    return httpx.HTTPStatusError(
        "upstream error", request=httpx.Request("POST", "http://x"), response=response
    )


@pytest.mark.asyncio
async def test_http_5xx_sets_result_unknown_with_idempotency(factory) -> None:
    """wedap 返回 5xx → RESULT_UNKNOWN，幂等 first_response 已落库。"""
    wedap = AsyncMock()
    wedap.submit_disbursement.side_effect = _make_http_status_error(503)
    result = await submit_order(factory, wedap_call=wedap.submit_disbursement, req=_req())
    assert result["txnStatus"] == "RESULT_UNKNOWN"
    assert result["bizSeqNo"] == _req().biz_seq_no
    async with factory() as s:
        order = (await s.execute(select(BankTxnOrder))).scalar_one()
        assert order.status == OrderStatus.RESULT_UNKNOWN
    # 幂等重放返回相同 first_response（验证 record_response 已落库）
    wedap2 = AsyncMock()
    replay = await submit_order(factory, wedap_call=wedap2.submit_disbursement, req=_req())
    assert replay["txnStatus"] == "RESULT_UNKNOWN"
    assert wedap2.submit_disbursement.await_count == 0


@pytest.mark.asyncio
async def test_http_4xx_sets_failed_with_error_code_and_idempotency(factory) -> None:
    """wedap 返回 4xx → FAILED + errorCode=HTTP_422，幂等 first_response 已落库。"""
    wedap = AsyncMock()
    wedap.submit_disbursement.side_effect = _make_http_status_error(422)
    result = await submit_order(factory, wedap_call=wedap.submit_disbursement, req=_req())
    assert result["txnStatus"] == "FAILED"
    assert result["errorCode"] == "HTTP_422"
    async with factory() as s:
        order = (await s.execute(select(BankTxnOrder))).scalar_one()
        assert order.status == OrderStatus.FAILED
    # 幂等重放返回相同 first_response（验证 record_response 已落库）
    wedap2 = AsyncMock()
    replay = await submit_order(factory, wedap_call=wedap2.submit_disbursement, req=_req())
    assert replay["txnStatus"] == "FAILED"
    assert wedap2.submit_disbursement.await_count == 0
