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
    """wedap 业务失败（v0.4.0/#82 起 422 + 业务码经 _unwrap 升格 WedapError）→
    FAILED + errorCode=业务码 + errorMsg 文案透传（截断 200，供上游展示/排障）。"""
    from app.clients.wedap import WedapError

    wedap = AsyncMock()
    wedap.submit_disbursement.side_effect = WedapError("422", "可用余额不足" + "x" * 300)
    result = await submit_order(factory, wedap_call=wedap.submit_disbursement, req=_req())
    assert result["txnStatus"] == "FAILED" and result["errorCode"] == "422"
    assert result["errorMsg"].startswith("可用余额不足")
    assert len(result["errorMsg"]) == 200
    # 幂等重放返回完整 first_response（含 errorMsg 全字段一致）且零外呼——
    # 钉住 errorMsg 经 record_response 落库/回放不丢不改（codex MEDIUM）。
    wedap2 = AsyncMock()
    replay = await submit_order(factory, wedap_call=wedap2.submit_disbursement, req=_req())
    assert replay == result
    assert wedap2.submit_disbursement.await_count == 0


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


# ── 提交响应最小字段契约：orderStatus（2026-07-22）─────────────────────────────


@pytest.mark.asyncio
async def test_order_status_reflects_terminal_and_unknown(factory) -> None:
    """orderStatus = CAS 后订单真实状态：同步 SUCCESS→SUCCEEDED；超时→RESULT_UNKNOWN；
    重放冻结返回同值。"""
    wedap = AsyncMock()
    wedap.submit_disbursement.return_value = {"txnStatus": "SUCCESS"}
    result = await submit_order(factory, wedap_call=wedap.submit_disbursement, req=_req())
    assert result["orderStatus"] == "SUCCEEDED"

    wedap_to = AsyncMock()
    wedap_to.submit_disbursement.side_effect = httpx.ConnectTimeout("t")
    req2 = _req(biz_seq_no="DSB-20260611-0001234567891")
    result2 = await submit_order(factory, wedap_call=wedap_to.submit_disbursement, req=req2)
    assert result2["orderStatus"] == "RESULT_UNKNOWN"

    # 重放：first_response 冻结含 orderStatus，零外呼
    wedap_replay = AsyncMock()
    replay = await submit_order(factory, wedap_call=wedap_replay.submit_disbursement, req=req2)
    assert replay == result2
    wedap_replay.submit_disbursement.assert_not_awaited()


@pytest.mark.asyncio
async def test_in_flight_replay_has_no_order_status(factory) -> None:
    """in-flight 重放（幂等行存在但无 first_response）：零查询路径不读 order 行，
    不带 orderStatus——inFlight=true 即「去查单」信号。"""
    from app.services.idempotency import check_or_register

    req = _req(biz_seq_no="DSB-20260611-0001234567892")
    async with factory() as session:
        async with session.begin():
            await check_or_register(
                session,
                tenant_id=req.tenant_id,
                business_scope=req.business_scope,
                idempotency_key=req.biz_seq_no,
                method="POST",
                path=req.business_scope,
                payload=req.wedap_payload,
            )
    wedap = AsyncMock()
    result = await submit_order(factory, wedap_call=wedap.submit_disbursement, req=req)
    assert result == {"txnStatus": "PROCESSING", "bizSeqNo": req.biz_seq_no, "inFlight": True}
    assert "orderStatus" not in result
    wedap.submit_disbursement.assert_not_awaited()


@pytest.mark.asyncio
async def test_null_txn_status_normalized_to_str(factory) -> None:
    """wedap 200 返回显式 txnStatus=null/非 str → 归一化为 str（防 response_model 把毒值
    升格 ResponseValidationError 500 且冻结进 first_response 致同 key 重放永久 500）。

    归一化目标 2026-08-10 由 PROCESSING 改为 RESULT_UNKNOWN：上游对 PROCESSING 走
    `is_ok=False` 且**回滚本地状态**，只有 RESULT_UNKNOWN 才挂起不回滚——毒响应下资金
    状态本就未知，绝不能让上游回滚。"""
    wedap = AsyncMock()
    wedap.submit_disbursement.return_value = {"txnStatus": None}
    req = _req(biz_seq_no="DSB-20260611-0001234567893")
    result = await submit_order(factory, wedap_call=wedap.submit_disbursement, req=req)
    assert result["txnStatus"] == "RESULT_UNKNOWN"
    assert result["orderStatus"] == "SUBMITTED"
    # 重放同样健康
    replay = await submit_order(factory, wedap_call=AsyncMock(), req=req)
    assert replay == result


# ── 还款（DTC 组合交易引擎）受理响应契约 · 对接文档 v0.5.0 §4.2 ──────────────────


def _repay_req(**kw) -> SubmitRequest:
    """还款受理请求：与 _req 同构，但走还款专用响应契约（repayment_contract=True）。"""
    d = dict(
        biz_seq_no="RPMT-20260810-0001234567890",
        business_action="REPAY",
        biz_type="RPMT",
        business_scope="p2p_repay",
        repayment_contract=True,
    )
    d.update(kw)
    return _req(**d)


@pytest.mark.asyncio
async def test_repayment_ack_success_maps_status_and_exposes_debt_settled(factory) -> None:
    """v0.5.0 起还款受理响应**不再返回 txnStatus**，改为
    `{bizSeqNo, globalTxId, status, detailStatus, debtSettled}`。

    共用 txnStatus 解析路径会读空 → 兜底 PROCESSING + 订单滞留 SUBMITTED，
    即「钱已落账却判处理中」的假挂起（本用例锁定修复）。北向 txnStatus 字段名保留
    （上游 lending-lifecycel 只读仓按此解析），wedap `status` 归一化映射进来。
    """
    wedap = AsyncMock()
    wedap.submit_repayment.return_value = {
        "bizSeqNo": "RPMT-20260810-0001234567890",
        "globalTxId": "GT20260727000001",
        "status": "SUCCESS",
        "detailStatus": "SUCCESS",
        "debtSettled": True,
    }
    result = await submit_order(factory, wedap_call=wedap.submit_repayment, req=_repay_req())
    assert result["txnStatus"] == "SUCCESS"
    assert result["orderStatus"] == OrderStatus.SUCCEEDED
    # 核销依据 + 排查锚点透传给上游（受理响应最小字段集之外的增量）
    assert result["debtSettled"] is True
    assert result["globalTxId"] == "GT20260727000001"
    assert result["detailStatus"] == "SUCCESS"
    async with factory() as s:
        assert (await s.execute(select(BankTxnOrder))).scalar_one().status == OrderStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_repayment_ack_processing_holds_order_and_debt_not_settled(factory) -> None:
    """status=PROCESSING（detailStatus 可为 PROCESSING/UNKNOWN/PENDING_MANUAL）→ 非终态：
    订单 SUBMITTED 挂起等轮询，debtSettled=False（⛔ 不得记账、不得回滚、不得重发）。"""
    wedap = AsyncMock()
    wedap.submit_repayment.return_value = {
        "bizSeqNo": "RPMT-20260810-0001234567891",
        "globalTxId": "GT20260727000002",
        "status": "PROCESSING",
        "detailStatus": "PENDING_MANUAL",
        "debtSettled": False,
    }
    result = await submit_order(
        factory,
        wedap_call=wedap.submit_repayment,
        req=_repay_req(biz_seq_no="RPMT-20260810-0001234567891"),
    )
    assert result["txnStatus"] == "PROCESSING"
    assert result["orderStatus"] == OrderStatus.SUBMITTED
    assert result["debtSettled"] is False
    assert result["detailStatus"] == "PENDING_MANUAL"


@pytest.mark.asyncio
async def test_repayment_ack_failed_is_terminal(factory) -> None:
    """status=FAILED = wedap 已确证零资金变动（借款人分文未扣）→ 终态 FAILED，可安全回滚。"""
    wedap = AsyncMock()
    wedap.submit_repayment.return_value = {
        "bizSeqNo": "RPMT-20260810-0001234567892",
        "globalTxId": "GT20260727000003",
        "status": "FAILED",
        "detailStatus": "FAILED",
        "debtSettled": False,
    }
    result = await submit_order(
        factory,
        wedap_call=wedap.submit_repayment,
        req=_repay_req(biz_seq_no="RPMT-20260810-0001234567892"),
    )
    assert result["txnStatus"] == "FAILED"
    assert result["orderStatus"] == OrderStatus.FAILED
    assert result["debtSettled"] is False


@pytest.mark.asyncio
async def test_repayment_ack_missing_status_normalized_to_result_unknown(factory) -> None:
    """还款响应缺 status / 非 str（毒响应）→ 北向归一化 RESULT_UNKNOWN + 订单 SUBMITTED。

    北向取 RESULT_UNKNOWN 而非 PROCESSING：后者会让上游回滚本地状态（只有前者挂起）。
    订单仍落 SUBMITTED 这一非终态，兜底 worker 照常收敛，两者语义不冲突。"""
    wedap = AsyncMock()
    wedap.submit_repayment.return_value = {"bizSeqNo": "x", "status": None}
    req = _repay_req(biz_seq_no="RPMT-20260810-0001234567893")
    result = await submit_order(factory, wedap_call=wedap.submit_repayment, req=req)
    assert result["txnStatus"] == "RESULT_UNKNOWN"
    assert result["orderStatus"] == OrderStatus.SUBMITTED
    assert result["debtSettled"] is False
    replay = await submit_order(factory, wedap_call=AsyncMock(), req=req)
    assert replay == result


@pytest.mark.asyncio
async def test_repayment_debt_settled_non_bool_normalized_false(factory) -> None:
    """debtSettled 非 bool（缺失 / "true" 字符串 / null）→ 归一化 False。

    金融安全的保守方向：宁可不核销（可人工补），不可误核销（债务凭空消失）。
    仅 JSON 真 boolean true 才认。
    """
    wedap = AsyncMock()
    wedap.submit_repayment.return_value = {
        "status": "SUCCESS",
        "detailStatus": "SUCCESS",
        "debtSettled": "true",
    }
    result = await submit_order(
        factory,
        wedap_call=wedap.submit_repayment,
        req=_repay_req(biz_seq_no="RPMT-20260810-0001234567894"),
    )
    assert result["debtSettled"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["6605U00900211", "6605B00900212"])
async def test_pending_business_codes_set_result_unknown(factory, code: str) -> None:
    """对接文档 v0.5.0 §4.2 业务错误码：`6605U00900211`(交易结果待确认) /
    `6605B00900212`(交易需人工处理) 的真实语义是**转轮询**，不是失败。

    旧行为把 HTTP200+业务码一律判 FAILED → 上游据此回滚，而此时 wedap 侧资金可能已变动
    或正在柜面人工处置中，违反 §3.6.1「只有确证 FAILED 才允许回滚」铁律（状态撕裂）。
    """
    from app.clients.wedap import WedapError

    wedap = AsyncMock()
    wedap.submit_repayment.side_effect = WedapError(code, "交易结果待确认")
    result = await submit_order(
        factory,
        wedap_call=wedap.submit_repayment,
        req=_repay_req(biz_seq_no=f"RPMT-2026081{code[-1]}-000123456789"),
    )
    assert result["txnStatus"] == "RESULT_UNKNOWN"
    assert result["orderStatus"] == OrderStatus.RESULT_UNKNOWN
    # 业务码保留供上游排障/展示（RESULT_UNKNOWN 也要能追溯到具体挂起原因）
    assert result["errorCode"] == code


@pytest.mark.asyncio
async def test_terminal_business_codes_still_failed(factory) -> None:
    """确证型业务拒绝（余额不足 6605B00900205 等）仍是终态 FAILED——零资金变动可安全回滚，
    不因 REQ-2 的待轮询豁免被误放宽。"""
    from app.clients.wedap import WedapError

    wedap = AsyncMock()
    wedap.submit_repayment.side_effect = WedapError("6605B00900205", "账户余额不足")
    result = await submit_order(
        factory,
        wedap_call=wedap.submit_repayment,
        req=_repay_req(biz_seq_no="RPMT-20260810-0001234567895"),
    )
    assert result["txnStatus"] == "FAILED"
    assert result["orderStatus"] == OrderStatus.FAILED
    assert result["errorCode"] == "6605B00900205"


@pytest.mark.asyncio
async def test_unknown_repayment_business_code_holds_instead_of_failing(factory) -> None:
    """**白名单制**：文档 13 码之外的未知业务码（wedap 新增而未同步通知）不得默认判 FAILED。

    两类误判代价不对称——未知码误判 FAILED → 上游回滚而 wedap 可能已扣款 = 资金错账（不可逆）；
    误判挂起 → 多等一轮 worker 查真实状态 = 延迟（可恢复）。故取代价小的一侧。
    """
    from app.clients.wedap import WedapError

    wedap = AsyncMock()
    wedap.submit_repayment.side_effect = WedapError("6605B00900299", "未来新增的未知业务码")
    result = await submit_order(
        factory,
        wedap_call=wedap.submit_repayment,
        req=_repay_req(biz_seq_no="RPMT-20260810-0001234567896"),
    )
    assert result["txnStatus"] == "RESULT_UNKNOWN"
    assert result["orderStatus"] == OrderStatus.RESULT_UNKNOWN
    assert result["errorCode"] == "6605B00900299"


@pytest.mark.asyncio
async def test_non_repayment_business_failure_unaffected_by_whitelist(factory) -> None:
    """白名单只作用于还款路径：其余交易（放款/归集/分发/退款/冲正）的业务失败保持既有
    终态 FAILED 语义，不因还款加固被连带放宽成挂起。"""
    from app.clients.wedap import WedapError

    wedap = AsyncMock()
    wedap.submit_disbursement.side_effect = WedapError("422", "可用余额不足")
    result = await submit_order(
        factory,
        wedap_call=wedap.submit_disbursement,
        req=_req(biz_seq_no="DSB-20260611-0001234567899"),
    )
    assert result["txnStatus"] == "FAILED"
    assert result["orderStatus"] == OrderStatus.FAILED


def test_repayment_terminal_reject_whitelist_matches_contract() -> None:
    """白名单内容对照契约而非实现：文档 v0.6.1 §4.2 的 10 个「受理即拒、零资金变动」码。

    刻意不断言集合大小或成员顺序——断言的是「211/212 待轮询码与 206（HTTP 500 路径）
    不在白名单内」这个不变量，防后人误把待轮询码加进来。
    """
    from app.domain.states import WEDAP_TERMINAL_REJECT_CODES, is_repayment_terminal_reject

    for pending in ("6605U00900211", "6605B00900212"):
        assert not is_repayment_terminal_reject(pending)
        assert pending not in WEDAP_TERMINAL_REJECT_CODES
    assert not is_repayment_terminal_reject("6605T00900206")
    for confirmed in ("6605B00900201", "6605B00900205", "6605B00900208", "6605B00900216"):
        assert is_repayment_terminal_reject(confirmed)
