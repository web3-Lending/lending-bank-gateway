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
