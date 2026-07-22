"""submit_reversal 单元测试：RVSL 单 SUCCEEDED + 原单同步翻 REVERSED。"""

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.clients.wedap import WedapError
from app.domain.states import OrderStatus
from app.models.base import Base
from app.models.txn import BankTxnOrder
from app.services.reversal import submit_reversal
from app.services.submit import SubmitRequest


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
            ori = (
                await s.execute(select(BankTxnOrder).where(BankTxnOrder.biz_seq_no == "CLT-KEEP"))
            ).scalar_one()
        assert ori.status == OrderStatus.SUCCEEDED  # 冲正失败，原单不翻

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
