"""南向 wedap 交易回调端点测试：inbox 三元组幂等 + after_ingest 接线点。

新增覆盖（T15 评审修复）：
- IntegrityError 收窄：非唯一约束的 IntegrityError → 500
- after_ingest 失败：行留 RECEIVED + error 非空，响应 200
- 重放再驱动：RECEIVED 行重放 → after_ingest 再执行 + 成功后行变 PROCESSED
- 已 PROCESSED 重放 → after_ingest 不再执行
- 最小 body 校验：缺 bizSeqNo / type → 400 GW_400_VALIDATION
"""

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.main import create_app
from app.models.base import Base
from app.models.callback import CallbackInbox

HEADERS = {
    "X-Caller-Service": "lifecycle",
    "X-Tenant-Id": "OCBC",
    "X-Request-Id": "cb-req-001",
}

BODY: dict[str, Any] = {
    "bizSeqNo": "BSQ-20260611-0001",
    "type": "LOAN_REPAYMENT",
    "txnId": "TXN-20260611-0001",
    "txnStatus": "SUCCESS",
    "amount": "100.0000",
    "currencyCode": "USD",
}


@pytest.fixture()
def client() -> TestClient:
    app = create_app()
    asyncio.run(_create_tables(app.state.engine))
    return TestClient(app)


async def _create_tables(engine: Any) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _query_inbox_row(engine: Any, tenant_id: str, request_id: str) -> CallbackInbox | None:
    async with engine.connect() as conn:
        result = await conn.execute(
            select(CallbackInbox).where(
                CallbackInbox.tenant_id == tenant_id,
                CallbackInbox.request_id == request_id,
            )
        )
        row = result.fetchone()
        return row  # type: ignore[return-value]


async def _query_inbox_rows(engine: Any, tenant_id: str) -> list[Any]:
    async with engine.connect() as conn:
        result = await conn.execute(
            select(CallbackInbox).where(CallbackInbox.tenant_id == tenant_id)
        )
        return list(result.fetchall())


# ---------------------------------------------------------------------------
# 原有测试（保持兼容，body 已含 bizSeqNo/type）
# ---------------------------------------------------------------------------


def test_first_receipt_200_and_db_row(client: TestClient) -> None:
    """首次接收：200 envelope，data.received=True, deduplicated=False；DB 落一行。"""
    r = client.post("/api/v1/callbacks/wedap/transactions", json=BODY, headers=HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert data["success"] is True
    assert data["data"]["received"] is True
    assert data["data"]["deduplicated"] is False
    assert data["trace_id"]

    rows = asyncio.run(
        _query_inbox_rows(client.app.state.engine, "OCBC")  # type: ignore[union-attr]
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.source == "WEDAP_TXN"  # type: ignore[union-attr]
    assert row.payload == BODY  # type: ignore[union-attr]


def test_same_request_id_dedup(client: TestClient) -> None:
    """相同 X-Request-Id 重放：deduplicated=True；DB 仍 1 行；after_ingest 只调用 1 次。"""
    spy = AsyncMock()
    client.app.state.callback_after_ingest = spy  # type: ignore[union-attr]

    # 第一次
    r1 = client.post("/api/v1/callbacks/wedap/transactions", json=BODY, headers=HEADERS)
    assert r1.status_code == 200
    assert r1.json()["data"]["deduplicated"] is False

    # 第二次（同 tenant + source + request_id，行已 PROCESSED）
    r2 = client.post("/api/v1/callbacks/wedap/transactions", json=BODY, headers=HEADERS)
    assert r2.status_code == 200
    assert r2.json()["data"]["deduplicated"] is True

    rows = asyncio.run(
        _query_inbox_rows(client.app.state.engine, "OCBC")  # type: ignore[union-attr]
    )
    assert len(rows) == 1

    # after_ingest 只被调用 1 次（第二次 PROCESSED 路径不再执行）
    assert spy.await_count == 1


def test_missing_tenant_id_400(client: TestClient) -> None:
    """缺 X-Tenant-Id → 400 GW_400_HEADER。"""
    h = {k: v for k, v in HEADERS.items() if k != "X-Tenant-Id"}
    r = client.post("/api/v1/callbacks/wedap/transactions", json=BODY, headers=h)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "GW_400_HEADER"


def test_missing_request_id_400(client: TestClient) -> None:
    """缺 X-Request-Id → 400 GW_400_HEADER。"""
    h = {k: v for k, v in HEADERS.items() if k != "X-Request-Id"}
    r = client.post("/api/v1/callbacks/wedap/transactions", json=BODY, headers=h)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "GW_400_HEADER"


def test_cross_tenant_same_request_id_no_dedup(client: TestClient) -> None:
    """不同 tenant 相同 request_id → 两行，不去重（三元组含 tenant_id）。"""
    r1 = client.post("/api/v1/callbacks/wedap/transactions", json=BODY, headers=HEADERS)
    assert r1.status_code == 200
    assert r1.json()["data"]["deduplicated"] is False

    other_tenant_headers = {**HEADERS, "X-Tenant-Id": "DBS"}
    r2 = client.post(
        "/api/v1/callbacks/wedap/transactions", json=BODY, headers=other_tenant_headers
    )
    assert r2.status_code == 200
    assert r2.json()["data"]["deduplicated"] is False

    rows_ocbc = asyncio.run(
        _query_inbox_rows(client.app.state.engine, "OCBC")  # type: ignore[union-attr]
    )
    assert len(rows_ocbc) == 1

    rows_dbs = asyncio.run(
        _query_inbox_rows(client.app.state.engine, "DBS")  # type: ignore[union-attr]
    )
    assert len(rows_dbs) == 1


def test_callback_exempt_from_s2s_missing_caller_allowed(client: TestClient) -> None:
    """回调端点已从 S2S 豁免（改用 apikey 守卫，FU-GW-INBOUND-AUTH-WEDAP-CALLBACK）：
    外部 wedap 无 lending S2S token，缺 X-Caller-Service 不再被 S2S 拦 401。
    apikey 认证的正/负路径见 tests/api/test_wedap_callback_auth.py。"""
    h = {k: v for k, v in HEADERS.items() if k != "X-Caller-Service"}
    r = client.post("/api/v1/callbacks/wedap/transactions", json=BODY, headers=h)
    assert r.status_code != 401


# ---------------------------------------------------------------------------
# T15 修复 1：IntegrityError 收窄
# ---------------------------------------------------------------------------


def test_non_unique_integrity_error_raises_500() -> None:
    """非唯一约束的 IntegrityError（FK/CHECK/NOT NULL 等）→ 500，不被吞为去重。

    TestClient 默认 raise_server_exceptions=True，会把未处理异常转回 Python 抛出；
    此处改为 False，让 Starlette _generic_exception_handler 把异常包成 JSON 500 响应。
    """
    from app.main import create_app

    app = create_app()
    asyncio.run(_create_tables(app.state.engine))

    # 构造一个消息不含唯一约束标识的 IntegrityError
    fake_orig = Exception("FOREIGN KEY constraint failed")
    fake_exc = IntegrityError("statement", {}, fake_orig)

    class _FakeSession:
        async def __aenter__(self) -> "_FakeSession":
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        def begin(self) -> "_FakeBegin":
            return _FakeBegin()

        def add(self, obj: Any) -> None:
            raise fake_exc

    class _FakeBegin:
        async def __aenter__(self) -> "_FakeBegin":
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            pass

    class _FakeFactory:
        def __call__(self) -> "_FakeSession":
            return _FakeSession()

    app.state.session_factory = _FakeFactory()

    # raise_server_exceptions=False：让 Starlette 异常 handler 把错误包成 JSON 500
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post("/api/v1/callbacks/wedap/transactions", json=BODY, headers=HEADERS)
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "GW_500_INTERNAL"


# ---------------------------------------------------------------------------
# T15 修复 2：after_ingest 失败补偿
# ---------------------------------------------------------------------------


def test_after_ingest_failure_returns_200_row_stays_received(client: TestClient) -> None:
    """after_ingest 抛错 → 响应 200 received=True；行 status=RECEIVED，error 非空。"""
    failing_spy = AsyncMock(side_effect=RuntimeError("downstream unavailable"))
    client.app.state.callback_after_ingest = failing_spy  # type: ignore[union-attr]

    r = client.post("/api/v1/callbacks/wedap/transactions", json=BODY, headers=HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert data["data"]["received"] is True
    assert data["data"]["deduplicated"] is False

    # 行 status 留 RECEIVED，error 非空
    row = asyncio.run(
        _query_inbox_row(client.app.state.engine, "OCBC", "cb-req-001")  # type: ignore[union-attr]
    )
    assert row is not None
    assert row.status == "RECEIVED"  # type: ignore[union-attr]
    assert row.error is not None and row.error != ""  # type: ignore[union-attr]


def test_after_ingest_success_row_becomes_processed(client: TestClient) -> None:
    """after_ingest 成功 → 行 status=PROCESSED。"""
    spy = AsyncMock()
    client.app.state.callback_after_ingest = spy  # type: ignore[union-attr]

    r = client.post("/api/v1/callbacks/wedap/transactions", json=BODY, headers=HEADERS)
    assert r.status_code == 200

    row = asyncio.run(
        _query_inbox_row(client.app.state.engine, "OCBC", "cb-req-001")  # type: ignore[union-attr]
    )
    assert row is not None
    assert row.status == "PROCESSED"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# T15 修复 2（续）：重放再驱动
# ---------------------------------------------------------------------------


def test_replay_received_row_drives_after_ingest_again(client: TestClient) -> None:
    """RECEIVED 行重放 → after_ingest 再次执行；成功后行变 PROCESSED。"""
    # 第一次：after_ingest 失败，行留 RECEIVED
    failing_spy = AsyncMock(side_effect=RuntimeError("first attempt failed"))
    client.app.state.callback_after_ingest = failing_spy  # type: ignore[union-attr]

    r1 = client.post("/api/v1/callbacks/wedap/transactions", json=BODY, headers=HEADERS)
    assert r1.status_code == 200

    row = asyncio.run(
        _query_inbox_row(client.app.state.engine, "OCBC", "cb-req-001")  # type: ignore[union-attr]
    )
    assert row is not None
    assert row.status == "RECEIVED"  # type: ignore[union-attr]

    # 第二次（重放）：after_ingest 成功
    success_spy = AsyncMock()
    client.app.state.callback_after_ingest = success_spy  # type: ignore[union-attr]

    r2 = client.post("/api/v1/callbacks/wedap/transactions", json=BODY, headers=HEADERS)
    assert r2.status_code == 200
    assert r2.json()["data"]["deduplicated"] is True

    # after_ingest 在重放时被调用了
    assert success_spy.await_count == 1

    # 行变 PROCESSED
    row2 = asyncio.run(
        _query_inbox_row(client.app.state.engine, "OCBC", "cb-req-001")  # type: ignore[union-attr]
    )
    assert row2 is not None
    assert row2.status == "PROCESSED"  # type: ignore[union-attr]


def test_replay_processed_row_no_after_ingest(client: TestClient) -> None:
    """已 PROCESSED 的行重放 → after_ingest 不再执行（幂等去重）。"""
    spy = AsyncMock()
    client.app.state.callback_after_ingest = spy  # type: ignore[union-attr]

    # 第一次正常落库并 PROCESSED
    r1 = client.post("/api/v1/callbacks/wedap/transactions", json=BODY, headers=HEADERS)
    assert r1.status_code == 200
    assert spy.await_count == 1

    # 第二次重放：行已 PROCESSED，不再驱动
    r2 = client.post("/api/v1/callbacks/wedap/transactions", json=BODY, headers=HEADERS)
    assert r2.status_code == 200
    assert r2.json()["data"]["deduplicated"] is True
    assert spy.await_count == 1  # 没有新增调用


# ---------------------------------------------------------------------------
# T15 修复 3：最小 body 校验
# ---------------------------------------------------------------------------


def test_missing_biz_seq_no_400(client: TestClient) -> None:
    """缺 bizSeqNo → 400 GW_400_VALIDATION，不落库。"""
    body = {k: v for k, v in BODY.items() if k != "bizSeqNo"}
    r = client.post("/api/v1/callbacks/wedap/transactions", json=body, headers=HEADERS)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "GW_400_VALIDATION"

    # 确认没有落库
    rows = asyncio.run(
        _query_inbox_rows(client.app.state.engine, "OCBC")  # type: ignore[union-attr]
    )
    assert len(rows) == 0


def test_missing_type_400(client: TestClient) -> None:
    """缺 type → 400 GW_400_VALIDATION，不落库。"""
    body = {k: v for k, v in BODY.items() if k != "type"}
    r = client.post("/api/v1/callbacks/wedap/transactions", json=body, headers=HEADERS)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "GW_400_VALIDATION"

    rows = asyncio.run(
        _query_inbox_rows(client.app.state.engine, "OCBC")  # type: ignore[union-attr]
    )
    assert len(rows) == 0


# ---------------------------------------------------------------------------
# 辅助函数直接单元测试（覆盖 async helper 内部行）
# ---------------------------------------------------------------------------


def test_get_inbox_row_returns_none_when_missing() -> None:
    """_get_inbox_row：行不存在时返回 None。"""
    from app.api.v1.callbacks import _get_inbox_row
    from app.models.base import Base

    async def _run() -> None:
        app = create_app()
        async with app.state.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        result = await _get_inbox_row(app.state.session_factory, "NO_TENANT", "NO_REQ")
        assert result is None

    asyncio.run(_run())


def test_set_and_get_inbox_row() -> None:
    """_set_inbox_status + _get_inbox_row：插入行后状态推进可被查到。"""
    from sqlalchemy import insert

    from app.api.v1.callbacks import _get_inbox_row, _set_inbox_status
    from app.models.base import Base

    async def _run() -> None:
        app = create_app()
        async with app.state.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(
                insert(CallbackInbox).values(
                    tenant_id="T1",
                    source="WEDAP_TXN",
                    request_id="R1",
                    payload={},
                    status="RECEIVED",
                )
            )
        await _set_inbox_status(
            app.state.session_factory, "T1", "R1", status="PROCESSED", error=None
        )
        row = await _get_inbox_row(app.state.session_factory, "T1", "R1")
        assert row is not None
        assert row.status == "PROCESSED"
        assert row.error is None

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 重放失败路径（覆盖 replay except Exception 分支）
# ---------------------------------------------------------------------------


def test_replay_received_row_after_ingest_fails_again(client: TestClient) -> None:
    """RECEIVED 行重放时 after_ingest 再次失败 → 200，行仍 RECEIVED，error 非空。"""
    # 第一次：after_ingest 失败，行留 RECEIVED
    failing_spy = AsyncMock(side_effect=RuntimeError("first attempt failed"))
    client.app.state.callback_after_ingest = failing_spy  # type: ignore[union-attr]

    r1 = client.post("/api/v1/callbacks/wedap/transactions", json=BODY, headers=HEADERS)
    assert r1.status_code == 200

    # 第二次（重放）：after_ingest 仍然失败
    client.app.state.callback_after_ingest = AsyncMock(  # type: ignore[union-attr]
        side_effect=RuntimeError("second attempt also failed")
    )

    r2 = client.post("/api/v1/callbacks/wedap/transactions", json=BODY, headers=HEADERS)
    assert r2.status_code == 200
    assert r2.json()["data"]["deduplicated"] is True

    # 行仍 RECEIVED（重放失败没有推进）
    row = asyncio.run(
        _query_inbox_row(client.app.state.engine, "OCBC", "cb-req-001")  # type: ignore[union-attr]
    )
    assert row is not None
    assert row.status == "RECEIVED"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# T17：after_ingest 真实执行时 outbox 行落库（target=lifecycle）
# ---------------------------------------------------------------------------


def test_after_ingest_enqueues_outbox_row(client: TestClient) -> None:
    """V2：终态回调 → apply_legs 聚合 SUCCEEDED → finalize 转发 outbox（稳定 dedup key）。

    同步优先 V2 后转发并入 apply_legs 的终态收口（finalize_terminal_in_session），仅终态转发，
    dedup_key 用业务稳定键 fwd-{tenant}-{biz}-{status}（替代易分叉的 fwd-{request_id}）。
    """
    from decimal import Decimal

    from sqlalchemy import select

    from app.models.callback import CallbackOutbox
    from app.models.txn import BankTxnOrder

    async def _seed() -> None:
        f = client.app.state.session_factory  # type: ignore[union-attr]
        async with f() as s:
            async with s.begin():
                s.add(
                    BankTxnOrder(
                        tenant_id="OCBC",
                        biz_seq_no="BSQ-20260611-0001",
                        business_action="REPAY",
                        biz_type="RPY",
                        amount=Decimal("100.0000"),
                        currency="USD",
                        caller_service="lifecycle",
                        status="SUBMITTED",
                    )
                )

    asyncio.run(_seed())

    wedap = AsyncMock()
    wedap.get_composite_steps.return_value = [
        {
            "stepSeq": 1,
            "sysRefNo": "REF-OUT-1",
            "stepType": "REPAYMENT",
            "amount": "100.0000",
            "currencyCode": "USD",
            "status": "SUCCESS",
            "txnDate": "20260611",
        }
    ]
    client.app.state.wedap = wedap  # type: ignore[union-attr]

    async def _query_outbox(engine: Any) -> list[Any]:
        async with engine.connect() as conn:
            result = await conn.execute(
                select(CallbackOutbox).where(CallbackOutbox.tenant_id == "OCBC")
            )
            return list(result.fetchall())

    r = client.post("/api/v1/callbacks/wedap/transactions", json=BODY, headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["data"]["received"] is True

    rows = asyncio.run(_query_outbox(client.app.state.engine))  # type: ignore[union-attr]
    assert len(rows) == 1
    assert rows[0].target == "lifecycle"  # type: ignore[union-attr]
    assert rows[0].dedup_key == "fwd-OCBC-BSQ-20260611-0001-SUCCEEDED"  # type: ignore[union-attr]


def test_after_ingest_atomic_enqueue_failure_rolls_back_legs(client: TestClient) -> None:
    """A-C-002 原子性：enqueue 失败 → 同事务回滚，leg 不落库、inbox 留 RECEIVED、outbox 0 行。

    旧实现 leg 同步与 enqueue 分两事务，leg 会先独立提交；改单事务后两者原子，enqueue 炸则
    leg 一并回滚。这正是「leg 已落库但 outbox 未入队」崩溃窗口被消除的体现。
    """
    from decimal import Decimal
    from unittest.mock import patch

    from sqlalchemy import func, select

    from app.models.callback import CallbackInbox, CallbackOutbox
    from app.models.txn import BankTxnLeg, BankTxnOrder

    biz = "DSB-20260611-0009000000001"
    body = {"bizSeqNo": biz, "type": "LOAN_DISBURSEMENT", "txnStatus": "SUCCESS"}
    headers = {**HEADERS, "X-Request-Id": "cb-atomic-001"}

    async def _seed() -> None:
        f = client.app.state.session_factory  # type: ignore[union-attr]
        async with f() as s:
            async with s.begin():
                s.add(
                    BankTxnOrder(
                        tenant_id="OCBC",
                        biz_seq_no=biz,
                        business_action="DISBURSE",
                        biz_type="DSB",
                        amount=Decimal("100.0000"),
                        currency="USD",
                        caller_service="lifecycle",
                        status="SUBMITTED",
                    )
                )

    asyncio.run(_seed())

    # wedap 返回一个合法 step（apply_legs 会想 upsert 一条 leg）；enqueue_forward 强制抛错
    wedap = AsyncMock()
    wedap.get_composite_steps.return_value = [
        {
            "stepSeq": 1,
            "sysRefNo": "REF-ATOMIC-1",
            "stepType": "DISBURSEMENT_COLLECTION",
            "amount": "100.0000",
            "currencyCode": "USD",
            "status": "SUCCESS",
            "txnDate": "20260611",
        }
    ]
    client.app.state.wedap = wedap  # type: ignore[union-attr]

    async def _count(engine: Any, model: Any) -> int:
        async with engine.connect() as conn:
            return int((await conn.execute(select(func.count()).select_from(model))).scalar_one())

    with patch(
        "app.services.order_finalize.enqueue_forward",
        new_callable=AsyncMock,
        side_effect=RuntimeError("enqueue boom"),
    ):
        r = client.post("/api/v1/callbacks/wedap/transactions", json=body, headers=headers)

    assert r.status_code == 200  # after_ingest 失败补偿仍 200

    engine = client.app.state.engine  # type: ignore[union-attr]
    assert asyncio.run(_count(engine, BankTxnLeg)) == 0  # leg 随同事务回滚
    assert asyncio.run(_count(engine, CallbackOutbox)) == 0  # outbox 无行

    async def _inbox_status() -> str:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    select(CallbackInbox.status).where(
                        CallbackInbox.tenant_id == "OCBC",
                        CallbackInbox.request_id == "cb-atomic-001",
                    )
                )
            ).scalar_one()
            return str(row)

    assert asyncio.run(_inbox_status()) == "RECEIVED"  # 未推进 PROCESSED，待重放


# ---------------------------------------------------------------------------
# A-C-001 修复：RECEIVED 行重放严格幂等——用首次落库 payload 再驱动，忽略漂移的本次 body
# ---------------------------------------------------------------------------


def test_replay_with_drifted_body_redrives_with_stored_payload(client: TestClient) -> None:
    """同 request_id 重发但 body 漂移：after_ingest 必须用首次落库的 payload 再驱动，而非本次 body。

    场景：上游复用 X-Request-Id 但发送了不同 body（金额/字段被改写）。inbox 行是权威记录，
    重放再驱动必须从既有 payload 收敛，否则 gateway 内部 leg/父单状态会被非首份 body 污染，
    与被 fwd-{request_id} 去重锁住的下游转发内容分叉。
    """
    # 第一次：after_ingest 失败，行留 RECEIVED（payload=首份 BODY）
    failing_spy = AsyncMock(side_effect=RuntimeError("first attempt failed"))
    client.app.state.callback_after_ingest = failing_spy  # type: ignore[union-attr]
    r1 = client.post("/api/v1/callbacks/wedap/transactions", json=BODY, headers=HEADERS)
    assert r1.status_code == 200

    # 第二次（重放）：同 request_id 但 body 漂移（金额与 txnId 被改写）；after_ingest 成功
    drifted_body = {**BODY, "amount": "999.9999", "txnId": "TXN-EVIL-0002"}
    assert drifted_body != BODY
    success_spy = AsyncMock()
    client.app.state.callback_after_ingest = success_spy  # type: ignore[union-attr]
    r2 = client.post("/api/v1/callbacks/wedap/transactions", json=drifted_body, headers=HEADERS)
    assert r2.status_code == 200
    assert r2.json()["data"]["deduplicated"] is True

    # 关键断言：after_ingest 用首次落库 payload（BODY）驱动，而非漂移的本次 body
    assert success_spy.await_count == 1
    assert success_spy.await_args is not None
    assert success_spy.await_args.kwargs["body"] == BODY
    assert success_spy.await_args.kwargs["body"] != drifted_body

    # 行推进 PROCESSED；落库 payload 仍是首份 BODY（重放不覆盖）
    row = asyncio.run(
        _query_inbox_row(client.app.state.engine, "OCBC", "cb-req-001")  # type: ignore[union-attr]
    )
    assert row is not None
    assert row.status == "PROCESSED"  # type: ignore[union-attr]
    assert row.payload == BODY  # type: ignore[union-attr]
