"""南向 wedap 交易回调端点测试：inbox 三元组幂等 + after_ingest 接线点。"""

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from app.models.base import Base
from app.models.callback import CallbackInbox

HEADERS = {
    "X-Caller-Service": "lifecycle",
    "X-Tenant-Id": "OCBC",
    "X-Request-Id": "cb-req-001",
}

BODY: dict[str, Any] = {
    "txnId": "TXN-20260611-0001",
    "txnStatus": "SUCCESS",
    "amount": "100.0000",
    "currencyCode": "USD",
}


@pytest.fixture()
def client() -> TestClient:
    app = create_app()
    asyncio.get_event_loop().run_until_complete(_create_tables(app.state.engine))
    return TestClient(app)


async def _create_tables(engine: Any) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _query_inbox_rows(engine: Any, tenant_id: str) -> list[CallbackInbox]:

    async with engine.connect() as conn:
        result = await conn.execute(
            select(CallbackInbox).where(CallbackInbox.tenant_id == tenant_id)
        )
        return list(result.fetchall())


def test_first_receipt_200_and_db_row(client: TestClient) -> None:
    """首次接收：200 envelope，data.received=True, deduplicated=False；DB 落一行。"""
    r = client.post("/api/v1/callbacks/wedap/transactions", json=BODY, headers=HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert data["success"] is True
    assert data["data"]["received"] is True
    assert data["data"]["deduplicated"] is False
    assert data["trace_id"]

    # DB assert：1 行，source=WEDAP_TXN，payload 完整
    rows = asyncio.get_event_loop().run_until_complete(
        _query_inbox_rows(client.app.state.engine, "OCBC")  # type: ignore[union-attr]
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.source == "WEDAP_TXN"  # type: ignore[union-attr]
    assert row.payload == BODY  # type: ignore[union-attr]


def test_same_request_id_dedup(client: TestClient) -> None:
    """相同 X-Request-Id 重放：返回 deduplicated=True；DB 仍只有 1 行；after_ingest 只调用 1 次。"""
    spy = AsyncMock()
    client.app.state.callback_after_ingest = spy  # type: ignore[union-attr]

    # 第一次
    r1 = client.post("/api/v1/callbacks/wedap/transactions", json=BODY, headers=HEADERS)
    assert r1.status_code == 200
    assert r1.json()["data"]["deduplicated"] is False

    # 第二次（同 tenant + source + request_id）
    r2 = client.post("/api/v1/callbacks/wedap/transactions", json=BODY, headers=HEADERS)
    assert r2.status_code == 200
    assert r2.json()["data"]["deduplicated"] is True

    # DB 仍只有 1 行
    rows = asyncio.get_event_loop().run_until_complete(
        _query_inbox_rows(client.app.state.engine, "OCBC")  # type: ignore[union-attr]
    )
    assert len(rows) == 1

    # after_ingest 只被调用 1 次
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

    # OCBC 1 行
    rows_ocbc = asyncio.get_event_loop().run_until_complete(
        _query_inbox_rows(client.app.state.engine, "OCBC")  # type: ignore[union-attr]
    )
    assert len(rows_ocbc) == 1

    # DBS 1 行
    rows_dbs = asyncio.get_event_loop().run_until_complete(
        _query_inbox_rows(client.app.state.engine, "DBS")  # type: ignore[union-attr]
    )
    assert len(rows_dbs) == 1


def test_missing_caller_service_401(client: TestClient) -> None:
    """缺 X-Caller-Service → S2SMiddleware 拦截 → 401。"""
    h = {k: v for k, v in HEADERS.items() if k != "X-Caller-Service"}
    r = client.post("/api/v1/callbacks/wedap/transactions", json=BODY, headers=h)
    assert r.status_code == 401
