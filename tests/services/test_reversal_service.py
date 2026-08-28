"""submit_reversal 单元测试：RVSL 单 SUCCEEDED + 原单同步翻 REVERSED。"""

import asyncio
import datetime as dt
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.clients.wedap import WedapError
from app.domain.states import OrderStatus
from app.models.base import Base
from app.models.txn import BankTxnOrder
from app.services.reversal import submit_reversal
from app.services.submit import SubmitRequest


async def _seed_reversed_collect(factory, biz: str) -> dt.datetime:
    """原单已是 REVERSED（幂等跳过场景种子）；返回种子 finalized_at 供后续对比未被二次收口。"""
    finalized_at = dt.datetime(2026, 7, 20, 3, 0, tzinfo=dt.UTC)
    async with factory() as s:
        async with s.begin():
            s.add(
                BankTxnOrder(
                    tenant_id="WBTHK01",
                    biz_seq_no=biz,
                    business_action="COLLECT",
                    biz_type="COLL",
                    amount=Decimal("5000.0000"),
                    currency="USD",
                    caller_service="liquidation",
                    status=OrderStatus.REVERSED,
                    request_id="req-collect-reversed",
                    trans_type="BANK_FUND_COLLECT_LOAN",
                    finalized_at=finalized_at,
                    finalized_via="SYNC",
                )
            )
    return finalized_at


@pytest.fixture()
def factory():  # type: ignore[no-untyped-def]
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async def _init() -> async_sessionmaker:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        return async_sessionmaker(engine, expire_on_commit=False)

    return asyncio.run(_init())


async def _seed_succeeded_collect(factory, biz: str) -> None:
    async with factory() as s:
        async with s.begin():
            s.add(
                BankTxnOrder(
                    tenant_id="WBTHK01",
                    biz_seq_no=biz,
                    business_action="COLLECT",
                    biz_type="COLL",
                    amount=Decimal("5000.0000"),
                    currency="USD",
                    caller_service="liquidation",
                    status=OrderStatus.SUCCEEDED,
                    request_id="req-collect",
                    trans_type="BANK_FUND_COLLECT_LOAN",
                )
            )


async def _seed_failed_collect(factory, biz: str) -> None:
    """原单处于不可迁移终态 FAILED（_ALLOWED[FAILED] = set()，不可再迁移到任何态）。"""
    async with factory() as s:
        async with s.begin():
            s.add(
                BankTxnOrder(
                    tenant_id="WBTHK01",
                    biz_seq_no=biz,
                    business_action="COLLECT",
                    biz_type="COLL",
                    amount=Decimal("5000.0000"),
                    currency="USD",
                    caller_service="liquidation",
                    status=OrderStatus.FAILED,
                    request_id="req-collect-failed",
                    trans_type="BANK_FUND_COLLECT_LOAN",
                )
            )


def _req(rvsl: str) -> SubmitRequest:
    return SubmitRequest(
        tenant_id="WBTHK01",
        biz_seq_no=rvsl,
        business_action="REVERSE",
        biz_type="RVSL",
        amount=Decimal("5000.0000"),
        currency="USD",
        caller_service="liquidation",
        request_id="req-rvsl",
        business_scope="bank_reversal",
        wedap_payload={"transType": "BANK_FUND_COLLECT_LOAN", "oriBizSeqNo": "CLT-1"},
        ori_req_date="20260722",
    )


def test_submit_reversal_flips_original_and_lands_rvsl_succeeded(factory) -> None:  # type: ignore[no-untyped-def]
    async def _run() -> None:
        await _seed_succeeded_collect(factory, "CLT-1")
        wedap_reverse = AsyncMock(return_value={"txnStatus": "REVERSED", "bizSeqNo": "RVSL-1"})
        resp = await submit_reversal(
            factory, wedap_reverse=wedap_reverse, req=_req("RVSL-1"), ori_biz_seq_no="CLT-1"
        )
        assert resp["txnStatus"] == "REVERSED"
        async with factory() as s:
            rvsl = (
                await s.execute(select(BankTxnOrder).where(BankTxnOrder.biz_seq_no == "RVSL-1"))
            ).scalar_one()
            ori = (
                await s.execute(select(BankTxnOrder).where(BankTxnOrder.biz_seq_no == "CLT-1"))
            ).scalar_one()
        assert rvsl.status == OrderStatus.SUCCEEDED  # 冲正指令受理成功
        assert ori.status == OrderStatus.REVERSED  # 原单被翻转

    asyncio.run(_run())


def test_submit_reversal_no_local_original_does_not_raise(factory) -> None:  # type: ignore[no-untyped-def]
    async def _run() -> None:
        wedap_reverse = AsyncMock(return_value={"txnStatus": "REVERSED"})
        resp = await submit_reversal(
            factory,
            wedap_reverse=wedap_reverse,
            req=_req("RVSL-NOORI"),
            ori_biz_seq_no="CLT-ABSENT",
        )
        assert resp["txnStatus"] == "REVERSED"
        async with factory() as s:
            rvsl = (
                await s.execute(select(BankTxnOrder).where(BankTxnOrder.biz_seq_no == "RVSL-NOORI"))
            ).scalar_one()
        assert rvsl.status == OrderStatus.SUCCEEDED

    asyncio.run(_run())


def test_submit_reversal_original_in_illegal_terminal_state_not_flipped(factory) -> None:  # type: ignore[no-untyped-def]
    """原单处于不可迁移终态（FAILED）时：wedap 冲正受理成功仍不炸请求、RVSL 单正常
    SUCCEEDED，但原单被 assert_transition + IllegalTransition 守卫挡住，保持 FAILED
    不被非法翻成 REVERSED（_reverse_original 的 try/except 覆盖路径）。"""

    async def _run() -> None:
        await _seed_failed_collect(factory, "CLT-FAILED")
        wedap_reverse = AsyncMock(return_value={"txnStatus": "REVERSED"})
        resp = await submit_reversal(
            factory,
            wedap_reverse=wedap_reverse,
            req=_req("RVSL-ILLEGAL"),
            ori_biz_seq_no="CLT-FAILED",
        )
        assert resp["txnStatus"] == "REVERSED"
        async with factory() as s:
            rvsl = (
                await s.execute(
                    select(BankTxnOrder).where(BankTxnOrder.biz_seq_no == "RVSL-ILLEGAL")
                )
            ).scalar_one()
            ori = (
                await s.execute(select(BankTxnOrder).where(BankTxnOrder.biz_seq_no == "CLT-FAILED"))
            ).scalar_one()
        assert rvsl.status == OrderStatus.SUCCEEDED  # RVSL 单不受影响，正常落终态
        assert ori.status == OrderStatus.FAILED  # 原单未被非法翻转，仍是 FAILED

    asyncio.run(_run())


def test_submit_reversal_wedap_error_fails_and_keeps_original(factory) -> None:  # type: ignore[no-untyped-def]
    async def _run() -> None:
        await _seed_succeeded_collect(factory, "CLT-KEEP")
        wedap_reverse = AsyncMock(side_effect=WedapError("BANK_08", "交易不存在"))
        resp = await submit_reversal(
            factory, wedap_reverse=wedap_reverse, req=_req("RVSL-ERR"), ori_biz_seq_no="CLT-KEEP"
        )
        assert resp["txnStatus"] == "FAILED"
        assert resp["errorCode"] == "BANK_08"
        async with factory() as s:
            rvsl = (
                await s.execute(select(BankTxnOrder).where(BankTxnOrder.biz_seq_no == "RVSL-ERR"))
            ).scalar_one()
            ori = (
                await s.execute(select(BankTxnOrder).where(BankTxnOrder.biz_seq_no == "CLT-KEEP"))
            ).scalar_one()
        assert rvsl.status == OrderStatus.FAILED  # RVSL 单落 FAILED
        assert ori.status == OrderStatus.SUCCEEDED  # 冲正失败，原单不翻

    asyncio.run(_run())


def test_submit_reversal_wedap_timeout_result_unknown_keeps_original(factory) -> None:  # type: ignore[no-untyped-def]
    """wedap 超时（RESULT_UNKNOWN）：RVSL 单落 RESULT_UNKNOWN，原单不翻——RESULT_UNKNOWN
    非冲正受理成功，_reverse_original 不该被调用（submit_reversal 里
    `if new_status == OrderStatus.SUCCEEDED` 守卫）。"""

    async def _run() -> None:
        await _seed_succeeded_collect(factory, "CLT-UNKNOWN")
        wedap_reverse = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        resp = await submit_reversal(
            factory,
            wedap_reverse=wedap_reverse,
            req=_req("RVSL-UNKNOWN"),
            ori_biz_seq_no="CLT-UNKNOWN",
        )
        assert resp["txnStatus"] == "RESULT_UNKNOWN"
        async with factory() as s:
            rvsl = (
                await s.execute(
                    select(BankTxnOrder).where(BankTxnOrder.biz_seq_no == "RVSL-UNKNOWN")
                )
            ).scalar_one()
            ori = (
                await s.execute(
                    select(BankTxnOrder).where(BankTxnOrder.biz_seq_no == "CLT-UNKNOWN")
                )
            ).scalar_one()
        assert rvsl.status == OrderStatus.RESULT_UNKNOWN  # RVSL 单落 RESULT_UNKNOWN
        assert ori.status == OrderStatus.SUCCEEDED  # 结果未知不得翻原单

    asyncio.run(_run())


def test_submit_reversal_idempotent_replay(factory) -> None:  # type: ignore[no-untyped-def]
    async def _run() -> None:
        await _seed_succeeded_collect(factory, "CLT-IDEM")
        wedap_reverse = AsyncMock(return_value={"txnStatus": "REVERSED"})
        r1 = await submit_reversal(
            factory, wedap_reverse=wedap_reverse, req=_req("RVSL-IDEM"), ori_biz_seq_no="CLT-IDEM"
        )
        r2 = await submit_reversal(
            factory, wedap_reverse=wedap_reverse, req=_req("RVSL-IDEM"), ori_biz_seq_no="CLT-IDEM"
        )
        assert r1["txnStatus"] == "REVERSED"
        assert r2 == r1  # 重放返回首次 response
        assert wedap_reverse.await_count == 1  # 零重复外呼

    asyncio.run(_run())


def test_submit_reversal_original_already_reversed_idempotent_skip(factory) -> None:  # type: ignore[no-untyped-def]
    """原单已是 REVERSED（此前一次冲正已收口）：再次对同一原单发起冲正时，
    _reverse_original 命中 `OrderStatus(ori.status) == REVERSED` 幂等分支直接 return——
    不重新 assert_transition、不重复 finalize（finalized_at 保持首次值不变）。"""

    async def _run() -> None:
        seeded_finalized_at = await _seed_reversed_collect(factory, "CLT-ALREADY-REVERSED")
        wedap_reverse = AsyncMock(return_value={"txnStatus": "REVERSED"})
        resp = await submit_reversal(
            factory,
            wedap_reverse=wedap_reverse,
            req=_req("RVSL-ALREADY-REVERSED"),
            ori_biz_seq_no="CLT-ALREADY-REVERSED",
        )
        assert resp["txnStatus"] == "REVERSED"
        async with factory() as s:
            rvsl = (
                await s.execute(
                    select(BankTxnOrder).where(BankTxnOrder.biz_seq_no == "RVSL-ALREADY-REVERSED")
                )
            ).scalar_one()
            ori = (
                await s.execute(
                    select(BankTxnOrder).where(BankTxnOrder.biz_seq_no == "CLT-ALREADY-REVERSED")
                )
            ).scalar_one()
        assert rvsl.status == OrderStatus.SUCCEEDED  # RVSL 单本身正常落终态
        assert ori.status == OrderStatus.REVERSED  # 原单仍是 REVERSED，未被重复处理
        # sqlite 往返丢 tzinfo，按 naive UTC 值比对
        assert ori.finalized_at == seeded_finalized_at.replace(tzinfo=None)

    asyncio.run(_run())


def test_submit_reversal_wedap_5xx_result_unknown_keeps_original(factory) -> None:  # type: ignore[no-untyped-def]
    """wedap 返回 5xx（HTTPStatusError）：视同结果未知，RVSL 落 RESULT_UNKNOWN，
    原单不翻——与超时/网络异常同一收口（_reverse_original 不被调用）。"""

    async def _run() -> None:
        await _seed_succeeded_collect(factory, "CLT-5XX")
        error = httpx.HTTPStatusError(
            "server error",
            request=httpx.Request("POST", "http://wedap.test/reverse"),
            response=httpx.Response(500),
        )
        wedap_reverse = AsyncMock(side_effect=error)
        resp = await submit_reversal(
            factory, wedap_reverse=wedap_reverse, req=_req("RVSL-5XX"), ori_biz_seq_no="CLT-5XX"
        )
        assert resp["txnStatus"] == "RESULT_UNKNOWN"
        async with factory() as s:
            rvsl = (
                await s.execute(select(BankTxnOrder).where(BankTxnOrder.biz_seq_no == "RVSL-5XX"))
            ).scalar_one()
            ori = (
                await s.execute(select(BankTxnOrder).where(BankTxnOrder.biz_seq_no == "CLT-5XX"))
            ).scalar_one()
        assert rvsl.status == OrderStatus.RESULT_UNKNOWN
        assert ori.status == OrderStatus.SUCCEEDED  # 结果未知不得翻原单

    asyncio.run(_run())


def test_submit_reversal_wedap_4xx_fails_with_http_error_code(factory) -> None:  # type: ignore[no-untyped-def]
    """wedap 返回 4xx（HTTPStatusError）：RVSL 落 FAILED，errorCode=HTTP_<code>，
    原单不翻。"""

    async def _run() -> None:
        await _seed_succeeded_collect(factory, "CLT-4XX")
        error = httpx.HTTPStatusError(
            "bad request",
            request=httpx.Request("POST", "http://wedap.test/reverse"),
            response=httpx.Response(422),
        )
        wedap_reverse = AsyncMock(side_effect=error)
        resp = await submit_reversal(
            factory, wedap_reverse=wedap_reverse, req=_req("RVSL-4XX"), ori_biz_seq_no="CLT-4XX"
        )
        assert resp["txnStatus"] == "FAILED"
        assert resp["errorCode"] == "HTTP_422"
        async with factory() as s:
            rvsl = (
                await s.execute(select(BankTxnOrder).where(BankTxnOrder.biz_seq_no == "RVSL-4XX"))
            ).scalar_one()
            ori = (
                await s.execute(select(BankTxnOrder).where(BankTxnOrder.biz_seq_no == "CLT-4XX"))
            ).scalar_one()
        assert rvsl.status == OrderStatus.FAILED
        assert ori.status == OrderStatus.SUCCEEDED  # 冲正失败，原单不翻

    asyncio.run(_run())


def test_submit_reversal_rvsl_cas_skip_when_already_advanced(factory) -> None:  # type: ignore[no-untyped-def]
    """事务2 CAS 防倒退分支：wedap 外呼期间（事务1 已落 ACCEPTED，事务2 尚未开启）RVSL 单
    被"外部"（模拟回调/兜底路径）直接推进到 SUCCEEDED；事务2 FOR UPDATE 读到时状态已非
    ACCEPTED，`if rvsl.status == OrderStatus.ACCEPTED` 为 False → 跳过状态覆写/finalize/
    原单翻转，只走 record_response，不抛异常。"""

    async def _run() -> None:
        await _seed_succeeded_collect(factory, "CLT-CAS")

        async def _race_then_reverse(**_kwargs: object) -> dict[str, object]:
            async with factory() as s:
                async with s.begin():
                    rvsl = (
                        await s.execute(
                            select(BankTxnOrder).where(BankTxnOrder.biz_seq_no == "RVSL-CAS")
                        )
                    ).scalar_one()
                    rvsl.status = OrderStatus.SUCCEEDED
            return {"txnStatus": "REVERSED"}

        wedap_reverse = AsyncMock(side_effect=_race_then_reverse)
        resp = await submit_reversal(
            factory, wedap_reverse=wedap_reverse, req=_req("RVSL-CAS"), ori_biz_seq_no="CLT-CAS"
        )
        assert resp["txnStatus"] == "REVERSED"
        # 提交响应契约：CAS skip 时 orderStatus = 外部已推进的真实状态
        assert resp["orderStatus"] == "SUCCEEDED"
        async with factory() as s:
            rvsl = (
                await s.execute(select(BankTxnOrder).where(BankTxnOrder.biz_seq_no == "RVSL-CAS"))
            ).scalar_one()
            ori = (
                await s.execute(select(BankTxnOrder).where(BankTxnOrder.biz_seq_no == "CLT-CAS"))
            ).scalar_one()
        assert rvsl.status == OrderStatus.SUCCEEDED  # 外部推进的状态被保留，未被覆写
        assert ori.status == OrderStatus.SUCCEEDED  # CAS skip 路径不触发原单翻转

    asyncio.run(_run())


def test_submit_reversal_null_txn_status_normalized(factory) -> None:  # type: ignore[no-untyped-def]
    """wedap 200 返回显式 txnStatus=null → 归一化 REVERSED（同 submit_order，独立评审 MEDIUM）：
    防 response_model 把毒值升格 500 且冻结进 first_response 致重放永久 500。"""

    async def _run() -> None:
        await _seed_succeeded_collect(factory, "CLT-NULLTS")
        wedap_reverse = AsyncMock(return_value={"txnStatus": None})
        resp = await submit_reversal(
            factory,
            wedap_reverse=wedap_reverse,
            req=_req("RVSL-NULLTS"),
            ori_biz_seq_no="CLT-NULLTS",
        )
        assert resp["txnStatus"] == "REVERSED"
        assert resp["orderStatus"] == "SUCCEEDED"

    asyncio.run(_run())


# ── MONEY_WRITE typed 字段（API 规范 v2.2 §8.2）· 2026-08-28 纯增量 ──────────────


def _typed(resp: dict) -> tuple:  # type: ignore[type-arg]
    return (
        resp.get("outcome"),
        resp.get("operationStatus"),
        resp.get("retryPolicy"),
        resp.get("resubmitAllowed"),
    )


def test_reversal_typed_fields_sync_success(factory) -> None:  # type: ignore[no-untyped-def]
    """冲正指令受理成功（RVSL SUCCEEDED）：outcome 省略 + SUCCEEDED/NEVER + 查单地址成对。

    冲正**不是**还款，statusUrl 必须走通用查单端点。
    """

    async def _run() -> None:
        await _seed_succeeded_collect(factory, "CLT-TYPED-OK")
        wedap_reverse = AsyncMock(return_value={"txnStatus": "REVERSED"})
        resp = await submit_reversal(
            factory,
            wedap_reverse=wedap_reverse,
            req=_req("RVSL-TYPED-OK"),
            ori_biz_seq_no="CLT-TYPED-OK",
        )
        assert "outcome" not in resp
        assert _typed(resp)[1:] == ("SUCCEEDED", "NEVER", False)
        assert resp["operationId"] == "RVSL-TYPED-OK"
        assert resp["statusUrl"] == "/api/v1/bank-funds/status?bizSeqNo=RVSL-TYPED-OK"

    asyncio.run(_run())


def test_reversal_typed_fields_timeout_is_unknown(factory) -> None:  # type: ignore[no-untyped-def]
    """冲正超时：指令可能已到 wedap → UNKNOWN 去查单，绝不 NOT_APPLIED。"""

    async def _run() -> None:
        await _seed_succeeded_collect(factory, "CLT-TYPED-TO")
        wedap_reverse = AsyncMock(side_effect=httpx.ConnectTimeout("t"))
        resp = await submit_reversal(
            factory,
            wedap_reverse=wedap_reverse,
            req=_req("RVSL-TYPED-TO"),
            ori_biz_seq_no="CLT-TYPED-TO",
        )
        assert _typed(resp) == ("UNKNOWN", "RECONCILING", "POLL_STATUS", False)

    asyncio.run(_run())


def test_reversal_typed_fields_5xx_is_unknown(factory) -> None:  # type: ignore[no-untyped-def]
    """上游 5xx：结果未知 → UNKNOWN。"""

    async def _run() -> None:
        await _seed_succeeded_collect(factory, "CLT-TYPED-5XX")
        wedap_reverse = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "boom",
                request=httpx.Request("POST", "http://x"),
                response=httpx.Response(502),
            )
        )
        resp = await submit_reversal(
            factory,
            wedap_reverse=wedap_reverse,
            req=_req("RVSL-TYPED-5XX"),
            ori_biz_seq_no="CLT-TYPED-5XX",
        )
        assert _typed(resp) == ("UNKNOWN", "RECONCILING", "POLL_STATUS", False)

    asyncio.run(_run())


def test_reversal_typed_fields_business_reject_is_not_applied(factory) -> None:  # type: ignore[no-untyped-def]
    """wedap 在 HTTP 4xx 上结构化拒绝：门口拒绝、原单也没翻 → 确证零影响。"""

    async def _run() -> None:
        await _seed_succeeded_collect(factory, "CLT-TYPED-ERR")
        wedap_reverse = AsyncMock(
            side_effect=WedapError("BANK-313", "原交易金额不符", http_status=422)
        )
        resp = await submit_reversal(
            factory,
            wedap_reverse=wedap_reverse,
            req=_req("RVSL-TYPED-ERR"),
            ori_biz_seq_no="CLT-TYPED-ERR",
        )
        assert _typed(resp) == ("NOT_APPLIED", "REJECTED", "CORRECT_AND_NEW_INTENT", False)
        async with factory() as s:
            ori = (
                await s.execute(
                    select(BankTxnOrder).where(BankTxnOrder.biz_seq_no == "CLT-TYPED-ERR")
                )
            ).scalar_one()
        assert ori.status == OrderStatus.SUCCEEDED  # 原单未翻 = NOT_APPLIED 的事实依据

    asyncio.run(_run())


def test_reversal_typed_fields_business_code_on_200_is_unknown(factory) -> None:  # type: ignore[no-untyped-def]
    """**BLOCKER-1 回归（冲正侧）**：HTTP 200 响应体里的业务码不是零影响证据 → UNKNOWN。

    通用冲正同样没有在册业务码表；`_unwrap` 在 envelope 漂移时抛的 `code="None"` 也走这条
    分支，判 NOT_APPLIED 会让上游据此回滚/换新键重发。
    """

    async def _run() -> None:
        await _seed_succeeded_collect(factory, "CLT-TYPED-C200")
        wedap_reverse = AsyncMock(side_effect=WedapError("None", "", http_status=200))
        resp = await submit_reversal(
            factory,
            wedap_reverse=wedap_reverse,
            req=_req("RVSL-TYPED-C200"),
            ori_biz_seq_no="CLT-TYPED-C200",
        )
        assert resp["orderStatus"] == "FAILED"  # 台账终态不变（既有行为）
        assert _typed(resp) == ("UNKNOWN", "RECONCILING", "POLL_STATUS", False)

    asyncio.run(_run())


def test_reversal_typed_fields_409_is_unknown(factory) -> None:  # type: ignore[no-untyped-def]
    """未在册的 4xx（409）不是门口拒绝证据 → UNKNOWN 去查单。"""

    async def _run() -> None:
        await _seed_succeeded_collect(factory, "CLT-TYPED-409")
        wedap_reverse = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "conflict",
                request=httpx.Request("POST", "http://x"),
                response=httpx.Response(409),
            )
        )
        resp = await submit_reversal(
            factory,
            wedap_reverse=wedap_reverse,
            req=_req("RVSL-TYPED-409"),
            ori_biz_seq_no="CLT-TYPED-409",
        )
        assert _typed(resp) == ("UNKNOWN", "RECONCILING", "POLL_STATUS", False)

    asyncio.run(_run())


def test_reversal_typed_fields_follow_ledger_when_cas_skips(factory) -> None:  # type: ignore[no-untyped-def]
    """CAS skip（tx1/tx2 之间 RVSL 单已被推到 REVERSED）：typed 字段跟台账，不跟本次 ack。

    与 submit 侧同型（tests/services/test_sync_terminal_v2.py），钉死
    `app/services/reversal.py` 里 `OrderStatus(rvsl.status)` 这条口径：改成跟本次外呼结果
    就会对一笔台账已终结的单播报本次的 4xx 结论。
    """

    async def _run() -> None:
        await _seed_succeeded_collect(factory, "CLT-TYPED-CAS")

        async def _wedap_reverse(**kw):  # type: ignore[no-untyped-def]
            # 模拟并发回调：在外呼（tx1 后 tx2 前）把 RVSL 单推到 REVERSED
            async with factory() as s:
                async with s.begin():
                    rvsl = (
                        await s.execute(
                            select(BankTxnOrder).where(BankTxnOrder.biz_seq_no == "RVSL-TYPED-CAS")
                        )
                    ).scalar_one()
                    rvsl.status = OrderStatus.REVERSED
                    rvsl.finalized_at = dt.datetime.now(dt.UTC)
                    rvsl.finalized_via = "CALLBACK"
            raise WedapError("BANK-313", "原交易金额不符", http_status=422)

        resp = await submit_reversal(
            factory,
            wedap_reverse=_wedap_reverse,
            req=_req("RVSL-TYPED-CAS"),
            ori_biz_seq_no="CLT-TYPED-CAS",
        )
        # 台账已 REVERSED（产生过资金影响再被撤回）→ 保守档 UNKNOWN；
        # 若跟本次 ack（门口拒绝证据）会错报 NOT_APPLIED/REJECTED。
        assert resp["orderStatus"] == "REVERSED"
        assert _typed(resp) == ("UNKNOWN", "RECONCILING", "POLL_STATUS", False)

    asyncio.run(_run())


def test_reversal_typed_fields_4xx_is_not_applied(factory) -> None:  # type: ignore[no-untyped-def]
    """门口拒绝的 4xx：冲正指令未进 wedap 业务引擎 → NOT_APPLIED。"""

    async def _run() -> None:
        await _seed_succeeded_collect(factory, "CLT-TYPED-4XX")
        wedap_reverse = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "bad",
                request=httpx.Request("POST", "http://x"),
                response=httpx.Response(400),
            )
        )
        resp = await submit_reversal(
            factory,
            wedap_reverse=wedap_reverse,
            req=_req("RVSL-TYPED-4XX"),
            ori_biz_seq_no="CLT-TYPED-4XX",
        )
        assert _typed(resp) == ("NOT_APPLIED", "REJECTED", "CORRECT_AND_NEW_INTENT", False)

    asyncio.run(_run())
