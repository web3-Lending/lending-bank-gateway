"""legs.py 服务层单元测试。

覆盖：
- 两条 leg 落库 + 父单推进 SUCCEEDED
- 再同步幂等（status 可更新）+ REVERSAL 追加 + 父单推进 REVERSED
- 未知 biz_seq_no → noop（不落 leg）
- 空 steps → 不动 leg、order 状态不变
- aggregate 抛 ValueError → 不崩、order 状态不变、logger.error 产生
- IllegalTransition（order 已 FAILED 又来 SUCCESS legs）→ 不崩、order 状态不变、logger.error 产生
- callbacks 端点 after_ingest 接线端到端：POST 回调 → legs 落库 → order 推进 → inbox PROCESSED
- external_ref 漂移：再同步时 sysRefNo 变化 → logger.warning 产生，external_ref 不被修改
- 终态 leg 防倒退：SUCCESS leg 再同步为 PENDING → 拒绝写入 + logger.warning
"""

import asyncio
import logging
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.db import build_engine, build_session_factory
from app.domain.states import OrderStatus
from app.models.base import Base
from app.models.txn import BankTxnLeg, BankTxnOrder
from app.services.legs import sync_legs_for

BIZ = "DSB-20260611-0001234567890"
STEP = {
    "stepType": "DISBURSEMENT_COLLECTION",
    "stepSeq": 1,
    "sysRefNo": "R1",
    "amount": "60.0000",
    "currencyCode": "USD",
    "payerAccount": "L1",
    "payeeAccount": "POOL",
    "status": "SUCCESS",
    "txnDate": "20260611",
}


@pytest.fixture()
async def factory():
    engine = build_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = build_session_factory(engine)
    async with f() as s:
        async with s.begin():
            s.add(
                BankTxnOrder(
                    tenant_id="OCBC",
                    biz_seq_no=BIZ,
                    business_action="DISBURSE",
                    biz_type="DSB",
                    amount=Decimal("120.0000"),
                    currency="USD",
                    caller_service="lifecycle",
                    status="SUBMITTED",
                )
            )
    yield f
    await engine.dispose()


def _wedap(steps: list[dict[str, Any]]) -> AsyncMock:
    m = AsyncMock()
    m.get_composite_steps.return_value = steps
    return m


# ---------------------------------------------------------------------------
# 原始任务要求的三条核心测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_legs_landed_and_order_succeeded(factory) -> None:
    steps = [
        STEP,
        {**STEP, "stepSeq": 2, "sysRefNo": "R2", "stepType": "DISBURSEMENT_DISTRIBUTION"},
    ]
    await sync_legs_for(factory, wedap=_wedap(steps), tenant_id="OCBC", biz_seq_no=BIZ)
    async with factory() as s:
        legs = (await s.execute(select(BankTxnLeg))).scalars().all()
        order = (await s.execute(select(BankTxnOrder))).scalar_one()
    assert len(legs) == 2 and order.status == OrderStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_resync_is_idempotent_and_reversal_appends(factory) -> None:
    await sync_legs_for(factory, wedap=_wedap([STEP]), tenant_id="OCBC", biz_seq_no=BIZ)
    steps2 = [
        {**STEP, "status": "REVERSED"},
        {**STEP, "stepSeq": 2, "sysRefNo": "R1-REV", "status": "REVERSAL"},
    ]
    await sync_legs_for(factory, wedap=_wedap(steps2), tenant_id="OCBC", biz_seq_no=BIZ)
    async with factory() as s:
        legs = (await s.execute(select(BankTxnLeg).order_by(BankTxnLeg.step_seq))).scalars().all()
        order = (await s.execute(select(BankTxnOrder))).scalar_one()
    assert len(legs) == 2
    assert legs[0].status == "REVERSED" and legs[1].status == "REVERSAL"
    assert order.status == OrderStatus.REVERSED


@pytest.mark.asyncio
async def test_unknown_order_is_noop(factory) -> None:
    await sync_legs_for(
        factory,
        wedap=_wedap([STEP]),
        tenant_id="OCBC",
        biz_seq_no="DSB-20260611-0000000000404",
    )
    async with factory() as s:
        legs = (await s.execute(select(BankTxnLeg))).scalars().all()
        assert legs == []


# ---------------------------------------------------------------------------
# 补充测试：空 steps → leg 不动、order 状态不变
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_steps_no_change(factory) -> None:
    """空 steps 列表：不插入任何 leg，order 状态维持 SUBMITTED。"""
    await sync_legs_for(factory, wedap=_wedap([]), tenant_id="OCBC", biz_seq_no=BIZ)
    async with factory() as s:
        legs = (await s.execute(select(BankTxnLeg))).scalars().all()
        order = (await s.execute(select(BankTxnOrder))).scalar_one()
    assert legs == []
    assert order.status == "SUBMITTED"


# ---------------------------------------------------------------------------
# 补充测试：aggregate 抛 ValueError → 不崩、order 状态不变、logger.error 产生
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aggregate_value_error_is_defended(factory, caplog) -> None:
    """aggregate_order_status 抛 ValueError（畸形 leg 组合）→ sync 不崩、order 状态不变。"""
    # 构造畸形数据：只有 REVERSED leg 没有 REVERSAL leg → aggregate 抛 ValueError
    reversed_step = {**STEP, "status": "REVERSED"}
    with caplog.at_level(logging.ERROR, logger="app.services.legs"):
        await sync_legs_for(
            factory, wedap=_wedap([reversed_step]), tenant_id="OCBC", biz_seq_no=BIZ
        )
    async with factory() as s:
        order = (await s.execute(select(BankTxnOrder))).scalar_one()
    # order 状态不应被推进（异常被防御）
    assert order.status == "SUBMITTED"
    # 必须有 logger.exception 产生
    assert any("sync_legs aggregate/transition rejected" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 补充测试：IllegalTransition → 不崩、order 状态不变、logger.error 产生
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_illegal_transition_is_defended(factory, caplog) -> None:
    """order 已 FAILED，又来全 SUCCESS legs → IllegalTransition，sync 不崩、order 状态不变。"""
    # 先把 order 推到 FAILED
    async with factory() as s:
        async with s.begin():
            order = (await s.execute(select(BankTxnOrder))).scalar_one()
            order.status = "FAILED"

    # 现在 sync 进来两条 SUCCESS leg，aggregate → SUCCEEDED；
    # FAILED→SUCCEEDED 不在 _ALLOWED 表内 → IllegalTransition
    steps = [
        STEP,
        {**STEP, "stepSeq": 2, "sysRefNo": "R2", "stepType": "DISBURSEMENT_DISTRIBUTION"},
    ]
    with caplog.at_level(logging.ERROR, logger="app.services.legs"):
        await sync_legs_for(factory, wedap=_wedap(steps), tenant_id="OCBC", biz_seq_no=BIZ)
    async with factory() as s:
        order = (await s.execute(select(BankTxnOrder))).scalar_one()
    assert order.status == "FAILED"
    assert any("sync_legs aggregate/transition rejected" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 补充测试：状态未变 → 不触发 assert_transition（覆盖 98->exit 分支）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_status_change_skips_transition(factory) -> None:
    """两条 SUCCESS leg 同步后 order=SUCCEEDED；再次同步相同 steps → new_status==SUCCEEDED
    == order.status，不进入 assert_transition 分支，order 状态仍 SUCCEEDED。"""
    steps = [
        STEP,
        {**STEP, "stepSeq": 2, "sysRefNo": "R2", "stepType": "DISBURSEMENT_DISTRIBUTION"},
    ]
    await sync_legs_for(factory, wedap=_wedap(steps), tenant_id="OCBC", biz_seq_no=BIZ)
    # 再次同步（状态未变）
    await sync_legs_for(factory, wedap=_wedap(steps), tenant_id="OCBC", biz_seq_no=BIZ)
    async with factory() as s:
        order = (await s.execute(select(BankTxnOrder))).scalar_one()
    assert order.status == OrderStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# 端到端：after_ingest 接线 → POST 回调 → legs 落库 → order 推进 → inbox PROCESSED
# ---------------------------------------------------------------------------


def test_after_ingest_wires_legs_and_advances_order() -> None:
    """callbacks 端点 after_ingest 接线后端到端。

    POST /api/v1/callbacks/wedap/transactions → after_ingest（真实实现）
    → sync_legs_for → legs 落库 → order 推进 SUCCEEDED → inbox.status == PROCESSED。
    """
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.models.base import Base
    from app.models.callback import CallbackInbox

    app = create_app()

    async def _setup() -> None:
        async with app.state.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        # 预插入 order
        f = app.state.session_factory
        async with f() as s:
            async with s.begin():
                s.add(
                    BankTxnOrder(
                        tenant_id="OCBC",
                        biz_seq_no=BIZ,
                        business_action="DISBURSE",
                        biz_type="DSB",
                        amount=Decimal("120.0000"),
                        currency="USD",
                        caller_service="lifecycle",
                        status="SUBMITTED",
                    )
                )

    asyncio.run(_setup())

    # 挂 wedap mock：返回两条 SUCCESS step
    mock_wedap = AsyncMock()
    mock_wedap.get_composite_steps.return_value = [
        STEP,
        {**STEP, "stepSeq": 2, "sysRefNo": "R2", "stepType": "DISBURSEMENT_DISTRIBUTION"},
    ]
    app.state.wedap = mock_wedap

    headers = {
        "X-Caller-Service": "lifecycle",
        "X-Tenant-Id": "OCBC",
        "X-Request-Id": "cb-e2e-001",
    }
    body = {
        "bizSeqNo": BIZ,
        "type": "LOAN_DISBURSEMENT",
        "txnStatus": "SUCCESS",
    }

    with TestClient(app) as client:
        r = client.post("/api/v1/callbacks/wedap/transactions", json=body, headers=headers)

    assert r.status_code == 200
    assert r.json()["data"]["received"] is True

    async def _verify() -> None:
        f = app.state.session_factory
        async with f() as s:
            legs = (await s.execute(select(BankTxnLeg))).scalars().all()
            order = (await s.execute(select(BankTxnOrder))).scalar_one()
            inbox_row = (
                await s.execute(
                    select(CallbackInbox).where(
                        CallbackInbox.tenant_id == "OCBC",
                        CallbackInbox.request_id == "cb-e2e-001",
                    )
                )
            ).scalar_one()
        assert len(legs) == 2
        assert order.status == OrderStatus.SUCCEEDED
        # Finding 2：inbox 行必须推进为 PROCESSED
        assert inbox_row.status == "PROCESSED"

    asyncio.run(_verify())


# ---------------------------------------------------------------------------
# Finding 3：external_ref 漂移告警
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_external_ref_drift_warns_and_does_not_update(factory, caplog) -> None:
    """再同步时 sysRefNo 与已有 external_ref 不同 → logger.warning 产生，external_ref 不被修改。"""
    # 先落一条 leg（sysRefNo="R1"）
    await sync_legs_for(factory, wedap=_wedap([STEP]), tenant_id="OCBC", biz_seq_no=BIZ)

    # 再同步：sysRefNo 变成 "R1-DRIFT"
    drifted = {**STEP, "sysRefNo": "R1-DRIFT"}
    with caplog.at_level(logging.WARNING, logger="app.services.legs"):
        await sync_legs_for(factory, wedap=_wedap([drifted]), tenant_id="OCBC", biz_seq_no=BIZ)

    async with factory() as s:
        legs = (await s.execute(select(BankTxnLeg))).scalars().all()

    # external_ref 不被修改，仍是原值
    assert len(legs) == 1
    assert legs[0].external_ref == "R1"

    # 必须产生漂移告警
    assert any("external_ref drift" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Finding 4：终态 leg 防倒退
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_leg_status_not_overwritten(factory, caplog) -> None:
    """SUCCESS（终态）leg 再同步为 PENDING → 拒绝写入，状态保持 SUCCESS，warning 产生。"""
    # 先落一条 SUCCESS leg
    await sync_legs_for(factory, wedap=_wedap([STEP]), tenant_id="OCBC", biz_seq_no=BIZ)

    # 再同步：status 变回 PENDING（倒退）
    downgrade = {**STEP, "status": "PENDING"}
    with caplog.at_level(logging.WARNING, logger="app.services.legs"):
        await sync_legs_for(factory, wedap=_wedap([downgrade]), tenant_id="OCBC", biz_seq_no=BIZ)

    async with factory() as s:
        legs = (await s.execute(select(BankTxnLeg))).scalars().all()

    # 状态不应被覆盖为 PENDING
    assert len(legs) == 1
    assert legs[0].status == "SUCCESS"

    # 必须产生终态防倒退告警
    assert any("terminal leg status overwrite rejected" in r.message for r in caplog.records)
