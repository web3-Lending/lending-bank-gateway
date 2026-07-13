"""legs.py 服务层单元测试。

覆盖：
- 两条 leg 落库 + 父单推进 SUCCEEDED
- 再同步幂等（status 可更新）+ REVERSAL 追加 + 父单推进 REVERSED
- 未知 biz_seq_no → noop（不落 leg）
- 空 steps → 不动 leg、order 状态不变
- aggregate 抛 ValueError → 抛 LegsSyncIncomplete、order 状态不变、logger.error 产生
- IllegalTransition（order 已 FAILED 又来 SUCCESS legs）→ 抛 LegsSyncIncomplete、order 状态不变
- callbacks 端点 after_ingest 接线端到端：POST 回调 → legs 落库 → order 推进 → inbox PROCESSED
- external_ref 漂移：再同步时 sysRefNo 变化 → logger.warning 产生，external_ref 不被修改
- 终态 leg 防倒退：SUCCESS leg 再同步为 PENDING → 拒绝写入 + logger.warning
- 失败不可封存：aggregate ValueError → inbox 留 RECEIVED + outbox 零行 + 响应 200
"""

import asyncio
import logging
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.core.db import build_engine, build_session_factory
from app.domain.states import OrderStatus
from app.models.base import Base
from app.models.txn import BankTxnLeg, BankTxnOrder
from app.services.legs import LegsSyncIncomplete, sync_legs_for

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
async def test_unknown_order_raises_legs_sync_incomplete(factory) -> None:
    """未知 biz_seq_no → 抛 LegsSyncIncomplete，零 leg 落库（不封存为已处理）。"""
    with pytest.raises(LegsSyncIncomplete, match="unknown order"):
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
# P2 修复：aggregate 抛 ValueError → 抛 LegsSyncIncomplete，order 状态不变
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aggregate_value_error_raises_legs_sync_incomplete(factory, caplog) -> None:
    """aggregate_order_status 抛 ValueError（畸形 leg 组合）→ sync 抛 LegsSyncIncomplete。"""
    # 构造畸形数据：只有 REVERSED leg 没有 REVERSAL leg → aggregate 抛 ValueError
    reversed_step = {**STEP, "status": "REVERSED"}
    with caplog.at_level(logging.ERROR, logger="app.services.legs"):
        with pytest.raises(LegsSyncIncomplete):
            await sync_legs_for(
                factory, wedap=_wedap([reversed_step]), tenant_id="OCBC", biz_seq_no=BIZ
            )
    async with factory() as s:
        order = (await s.execute(select(BankTxnOrder))).scalar_one()
    # order 状态不应被推进（异常被抛出前事务已回滚）
    assert order.status == "SUBMITTED"
    # 必须有 logger.exception 产生
    assert any("sync_legs aggregate/transition rejected" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# P2 修复：IllegalTransition → 抛 LegsSyncIncomplete，order 状态不变
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_illegal_transition_raises_legs_sync_incomplete(factory, caplog) -> None:
    """order 已 FAILED，又来全 SUCCESS legs → IllegalTransition → 抛 LegsSyncIncomplete。"""
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
        with pytest.raises(LegsSyncIncomplete):
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

    async def _noop_forever(*_args: object, **_kwargs: object) -> None:
        await asyncio.sleep(9999)

    with (
        patch("app.workers.outbox_dispatcher.run_forever", side_effect=_noop_forever),
        patch("app.workers.recon_worker.run_forever", side_effect=_noop_forever),
        TestClient(app) as client,
    ):
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


# ---------------------------------------------------------------------------
# P2：失败不可封存 — aggregate ValueError → inbox 留 RECEIVED + outbox 零行 + 响应 200
# ---------------------------------------------------------------------------


def test_sync_failure_leaves_inbox_received_and_no_outbox() -> None:
    """sync_legs_for 抛 LegsSyncIncomplete → _after_ingest 中断 outbox enqueue，
    inbox 行留 RECEIVED（status 不推进 PROCESSED），outbox 零行，响应仍 200。
    """
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.models.base import Base
    from app.models.callback import CallbackInbox, CallbackOutbox

    app = create_app()

    async def _setup() -> None:
        async with app.state.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        # 预插入 order（SUBMITTED）
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

    # wedap 返回畸形数据：只有 REVERSED leg，没有配对 REVERSAL → aggregate 抛 ValueError
    mock_wedap = AsyncMock()
    mock_wedap.get_composite_steps.return_value = [{**STEP, "status": "REVERSED"}]
    app.state.wedap = mock_wedap

    headers = {
        "X-Caller-Service": "lifecycle",
        "X-Tenant-Id": "OCBC",
        "X-Request-Id": "cb-fail-001",
    }
    body = {
        "bizSeqNo": BIZ,
        "type": "LOAN_DISBURSEMENT",
        "txnStatus": "SUCCESS",
    }

    async def _noop_forever(*_args: object, **_kwargs: object) -> None:
        await asyncio.sleep(9999)

    with (
        patch("app.workers.outbox_dispatcher.run_forever", side_effect=_noop_forever),
        patch("app.workers.recon_worker.run_forever", side_effect=_noop_forever),
        TestClient(app) as client,
    ):
        r = client.post("/api/v1/callbacks/wedap/transactions", json=body, headers=headers)

    # 响应仍 200（after_ingest 失败补偿）
    assert r.status_code == 200
    assert r.json()["data"]["received"] is True

    async def _verify() -> None:
        f = app.state.session_factory
        async with f() as s:
            inbox_row = (
                await s.execute(
                    select(CallbackInbox).where(
                        CallbackInbox.tenant_id == "OCBC",
                        CallbackInbox.request_id == "cb-fail-001",
                    )
                )
            ).scalar_one()
            outbox_rows = (
                (await s.execute(select(CallbackOutbox).where(CallbackOutbox.tenant_id == "OCBC")))
                .scalars()
                .all()
            )
        # inbox 留 RECEIVED（未推进）
        assert inbox_row.status == "RECEIVED"
        # outbox 零行（enqueue 被中断）
        assert len(outbox_rows) == 0

    asyncio.run(_verify())


# ---------------------------------------------------------------------------
# P1-2（终审）：未知 bizSeqNo 回调 → inbox 留 RECEIVED + outbox 零行
# ---------------------------------------------------------------------------


def test_unknown_biz_seq_no_callback_leaves_inbox_received_no_outbox() -> None:
    """未知 bizSeqNo 回调 → LegsSyncIncomplete 中断转发：
    - 响应 200 received=True
    - inbox.status == RECEIVED（未封存为已处理）
    - inbox.error 非空（留痕）
    - outbox 零行（不向 lifecycle 转发）
    """
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.models.base import Base
    from app.models.callback import CallbackInbox, CallbackOutbox

    UNKNOWN_BIZ = "DSB-20260611-0000000000404"

    app = create_app()

    async def _setup() -> None:
        async with app.state.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        # 故意不预插入 order，模拟未知/乱序回调

    asyncio.run(_setup())

    # wedap 返回正常 step（order 不存在时 sync_legs_for 应抛 LegsSyncIncomplete）
    mock_wedap = AsyncMock()
    mock_wedap.get_composite_steps.return_value = [STEP]
    app.state.wedap = mock_wedap

    headers = {
        "X-Caller-Service": "lifecycle",
        "X-Tenant-Id": "OCBC",
        "X-Request-Id": "cb-unknown-biz-001",
    }
    body = {
        "bizSeqNo": UNKNOWN_BIZ,
        "type": "LOAN_DISBURSEMENT",
        "txnStatus": "SUCCESS",
    }

    async def _noop_forever(*_args: object, **_kwargs: object) -> None:
        await asyncio.sleep(9999)

    with (
        patch("app.workers.outbox_dispatcher.run_forever", side_effect=_noop_forever),
        patch("app.workers.recon_worker.run_forever", side_effect=_noop_forever),
        TestClient(app) as client,
    ):
        r = client.post("/api/v1/callbacks/wedap/transactions", json=body, headers=headers)

    assert r.status_code == 200
    assert r.json()["data"]["received"] is True

    async def _verify() -> None:
        f = app.state.session_factory
        async with f() as s:
            inbox_row = (
                await s.execute(
                    select(CallbackInbox).where(
                        CallbackInbox.tenant_id == "OCBC",
                        CallbackInbox.request_id == "cb-unknown-biz-001",
                    )
                )
            ).scalar_one()
            outbox_rows = (
                (await s.execute(select(CallbackOutbox).where(CallbackOutbox.tenant_id == "OCBC")))
                .scalars()
                .all()
            )
        # 未知 order 不得封存为 PROCESSED
        assert inbox_row.status == "RECEIVED"
        # error 字段必须留痕（LegsSyncIncomplete 消息）
        assert inbox_row.error is not None and inbox_row.error != ""
        # outbox 零行：不向 lifecycle 转发
        assert len(outbox_rows) == 0

    asyncio.run(_verify())


# ---------------------------------------------------------------------------
# A-m-004：CLT（归集）单虽无 wedap 实时状态查询，回调链仍能驱动其至终态
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clt_order_driven_to_terminal_by_callback(factory) -> None:
    """归集（biz_type=CLT）单无 wedap status API，但回调 sync_legs 聚合与 biz_type 无关，
    仍能把 CLT 单驱动至终态 SUCCEEDED（A-m-004：CLT 靠回调/对账收敛，回调链覆盖终态）。"""
    clt_biz = "CLT-20260611-0002000000777"
    async with factory() as s:
        async with s.begin():
            s.add(
                BankTxnOrder(
                    tenant_id="OCBC",
                    biz_seq_no=clt_biz,
                    business_action="COLLECT",
                    biz_type="CLT",
                    amount=Decimal("60.0000"),
                    currency="USD",
                    caller_service="lifecycle",
                    status="SUBMITTED",
                )
            )

    steps = [{**STEP, "sysRefNo": "CLT-R1", "stepType": "COLLECTION", "status": "SUCCESS"}]
    await sync_legs_for(factory, wedap=_wedap(steps), tenant_id="OCBC", biz_seq_no=clt_biz)

    async with factory() as s:
        order = (
            await s.execute(select(BankTxnOrder).where(BankTxnOrder.biz_seq_no == clt_biz))
        ).scalar_one()
    assert order.status == OrderStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# G1：txn_date 拒单兜底 —— 上游缺/畸形字段 → 包装 LegsSyncIncomplete（非裸异常逸出 worker）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_leg_missing_txn_date_raises_legs_sync_incomplete(factory) -> None:
    """G1：新 leg 插入缺 txnDate → 抛 LegsSyncIncomplete（不再静默落 NULL），零 leg 落库。"""
    step_no_date = {k: v for k, v in STEP.items() if k != "txnDate"}
    with pytest.raises(LegsSyncIncomplete):
        await sync_legs_for(factory, wedap=_wedap([step_no_date]), tenant_id="OCBC", biz_seq_no=BIZ)
    async with factory() as s:
        legs = (await s.execute(select(BankTxnLeg))).scalars().all()
        order = (await s.execute(select(BankTxnOrder))).scalar_one()
    assert legs == []  # 整批回滚，不落部分 leg
    assert order.status == "SUBMITTED"  # 父单不推进


@pytest.mark.asyncio
async def test_new_leg_malformed_amount_raises_legs_sync_incomplete(factory) -> None:
    """G1：新 leg amount 非法（Decimal 抛 InvalidOperation）→ 包装 LegsSyncIncomplete，不逸出。"""
    step_bad_amount = {**STEP, "amount": "not-a-number"}
    with pytest.raises(LegsSyncIncomplete):
        await sync_legs_for(
            factory, wedap=_wedap([step_bad_amount]), tenant_id="OCBC", biz_seq_no=BIZ
        )
    async with factory() as s:
        legs = (await s.execute(select(BankTxnLeg))).scalars().all()
    assert legs == []


@pytest.mark.asyncio
async def test_existing_leg_status_update_without_txn_date_ok(factory) -> None:
    """G1 回归：txnDate 仅新 leg 插入必填；既有 leg 的状态更新步不携带 txnDate 也不应被拒。"""
    # 先落 leg（带 txnDate）
    await sync_legs_for(factory, wedap=_wedap([STEP]), tenant_id="OCBC", biz_seq_no=BIZ)
    # 再同步：仅 stepSeq+sysRefNo+status（无 txnDate/amount/stepType）→ 更新分支，不应抛
    update_step = {"stepSeq": 1, "sysRefNo": "R1", "status": "SUCCESS"}
    await sync_legs_for(factory, wedap=_wedap([update_step]), tenant_id="OCBC", biz_seq_no=BIZ)
    async with factory() as s:
        leg = (await s.execute(select(BankTxnLeg))).scalar_one()
    assert leg.txn_date == "20260611"  # 既有 txn_date 保留


@pytest.mark.asyncio
async def test_common_field_malformed_raises_legs_sync_incomplete(factory) -> None:
    """G1：公共字段畸形（stepSeq 非整数）→ int() 抛 ValueError → 包装 LegsSyncIncomplete。"""
    bad = {**STEP, "stepSeq": "not-an-int"}
    with pytest.raises(LegsSyncIncomplete):
        await sync_legs_for(factory, wedap=_wedap([bad]), tenant_id="OCBC", biz_seq_no=BIZ)
    async with factory() as s:
        legs = (await s.execute(select(BankTxnLeg))).scalars().all()
    assert legs == []


@pytest.mark.asyncio
async def test_new_leg_empty_txn_date_raises_legs_sync_incomplete(factory) -> None:
    """G1：新 leg txnDate 为空串 → 显式 raise → 包装 LegsSyncIncomplete（空值等同缺失）。"""
    empty = {**STEP, "txnDate": ""}
    with pytest.raises(LegsSyncIncomplete):
        await sync_legs_for(factory, wedap=_wedap([empty]), tenant_id="OCBC", biz_seq_no=BIZ)
    async with factory() as s:
        legs = (await s.execute(select(BankTxnLeg))).scalars().all()
    assert legs == []


@pytest.mark.asyncio
async def test_empty_sys_ref_no_raises_legs_sync_incomplete(factory) -> None:
    """G1：公共必填 sysRefNo 为空串 → 拒单（非空校验），零 leg 落库。"""
    with pytest.raises(LegsSyncIncomplete):
        await sync_legs_for(
            factory, wedap=_wedap([{**STEP, "sysRefNo": ""}]), tenant_id="OCBC", biz_seq_no=BIZ
        )
    async with factory() as s:
        assert (await s.execute(select(BankTxnLeg))).scalars().all() == []


@pytest.mark.asyncio
async def test_new_leg_empty_step_type_raises_legs_sync_incomplete(factory) -> None:
    """G1：新 leg 必填 stepType 为空串 → 拒单，零 leg 落库。"""
    with pytest.raises(LegsSyncIncomplete):
        await sync_legs_for(
            factory, wedap=_wedap([{**STEP, "stepType": ""}]), tenant_id="OCBC", biz_seq_no=BIZ
        )
    async with factory() as s:
        assert (await s.execute(select(BankTxnLeg))).scalars().all() == []
