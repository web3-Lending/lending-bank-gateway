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


def test_missing_caller_service_401(client: TestClient) -> None:
    """缺 X-Caller-Service → S2SMiddleware 拦截 → 401。"""
    h = {k: v for k, v in HEADERS.items() if k != "X-Caller-Service"}
    r = client.post("/api/v1/callbacks/wedap/transactions", json=BODY, headers=h)
    assert r.status_code == 401


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
