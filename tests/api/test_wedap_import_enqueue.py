import asyncio

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models.base import Base

HEADERS = {
    "X-Tenant-Id": "WBTHK01",
    "X-Request-Id": "wedap-import-BATCH-LEN-20260624-001",
    "X-Caller-Service": "lending-recon",  # S2S 调用方（recon→gateway）
}

BODY = {
    "import_batch_no": "BATCH-LEN-20260624-001",
    "data_type": "interest-accrual",
    "import_date": "20260624",
    "staging_key": "staging/20260624/interest-accrual.jsonl",
    "file_checksum": "a" * 64,
    "file_size": 128,
    "total_count": 3,
}


async def _create_tables(engine) -> None:  # type: ignore[no-untyped-def]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.fixture()
def client() -> TestClient:
    app = create_app()
    asyncio.run(_create_tables(app.state.engine))
    return TestClient(app)


def test_enqueue_accepts_and_returns_pending(client: TestClient) -> None:
    r = client.post("/api/v1/wedap/import/enqueue", json=BODY, headers=HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert data["importBatchNo"] == "BATCH-LEN-20260624-001"
    assert data["requestId"] == "wedap-import-BATCH-LEN-20260624-001"
    assert data["status"] == "PENDING"
    assert data["taskId"] is not None


def test_enqueue_idempotent_same_batch(client: TestClient) -> None:
    r1 = client.post("/api/v1/wedap/import/enqueue", json=BODY, headers=HEADERS)
    r2 = client.post("/api/v1/wedap/import/enqueue", json=BODY, headers=HEADERS)
    assert r1.json()["taskId"] == r2.json()["taskId"]  # 同号→返回既有任务


def test_enqueue_missing_tenant_header_400(client: TestClient) -> None:
    # 过 S2S（带 caller）但缺 X-Tenant-Id → require_headers 400
    r = client.post(
        "/api/v1/wedap/import/enqueue",
        json=BODY,
        headers={"X-Request-Id": "x", "X-Caller-Service": "lending-recon"},
    )
    assert r.status_code == 400


def test_enqueue_bad_checksum_length_422(client: TestClient) -> None:
    bad = {**BODY, "file_checksum": "short"}
    r = client.post("/api/v1/wedap/import/enqueue", json=bad, headers=HEADERS)
    assert r.status_code == 422  # pydantic 校验 SHA-256 64 位
