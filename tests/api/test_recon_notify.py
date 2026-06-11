"""M5 对账通知端点测试：X-Request-Id 幂等/契约校验/task NOTIFIED 落库。

契约（2026-06-08 v1.0.0）：
- Header：X-Request-Id=recon-result-{taskNo}-v{version}、X-Tenant-Id
- Body：reconDate(yyyyMMdd)/tenantId/s3Bucket/files[{fileName,s3Key,md5,totalCount}]
- 重发幂等：deduplicated=True，DB 不增行
"""

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.main import create_app
from app.models.base import Base
from app.models.callback import CallbackInbox
from app.models.recon import ReconResultTask

# ---------------------------------------------------------------------------
# 标准 Headers / Body（wedap 契约示例）
# ---------------------------------------------------------------------------

HEADERS = {
    "X-Caller-Service": "wedap",
    "X-Tenant-Id": "OCBC",
    "X-Request-Id": "recon-result-RECON-OCBC-20260604-v2",
    "X-Trace-Id": "trace-recon-001",
}

BODY: dict[str, Any] = {
    "reconDate": "20260604",
    "tenantId": "OCBC",
    "s3Bucket": "wedap-recon-bucket",
    "files": [
        {
            "fileName": "RECON_RESULT_RECON-OCBC-20260604_v2.xlsx",
            "s3Key": "OCBC/20260604/RECON_RESULT_RECON-OCBC-20260604_v2.xlsx",
            "md5": "A1B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4",
            "totalCount": 0,
        }
    ],
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client() -> TestClient:
    app = create_app()
    asyncio.run(_create_tables(app.state.engine))
    return TestClient(app)


async def _create_tables(engine: Any) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _query_inbox_rows(engine: Any, tenant_id: str, source: str) -> list[Any]:
    async with engine.connect() as conn:
        result = await conn.execute(
            select(CallbackInbox).where(
                CallbackInbox.tenant_id == tenant_id,
                CallbackInbox.source == source,
            )
        )
        return list(result.fetchall())


async def _query_recon_task_rows(engine: Any, tenant_id: str) -> list[Any]:
    async with engine.connect() as conn:
        result = await conn.execute(
            select(ReconResultTask).where(ReconResultTask.tenant_id == tenant_id)
        )
        return list(result.fetchall())


# ---------------------------------------------------------------------------
# 1. 首收 200：taskNo/version/deduplicated=False；DB inbox + recon_result_task 各一行
# ---------------------------------------------------------------------------


def test_first_receipt_200_and_db_rows(client: TestClient) -> None:
    """首次接收：200 envelope；data 含 taskNo/version/deduplicated=False；
    DB inbox(source=WEDAP_RECON) + recon_result_task(NOTIFIED, md5小写, diff_count) 各一行。
    """
    r = client.post("/api/v1/recon/notify", json=BODY, headers=HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert data["success"] is True
    assert data["data"]["taskNo"] == "RECON-OCBC-20260604"
    assert data["data"]["version"] == 2
    assert data["data"]["deduplicated"] is False
    assert data["trace_id"]

    # DB: inbox 一行，source=WEDAP_RECON
    inbox_rows = asyncio.run(
        _query_inbox_rows(client.app.state.engine, "OCBC", "WEDAP_RECON")  # type: ignore[union-attr]
    )
    assert len(inbox_rows) == 1
    assert inbox_rows[0].source == "WEDAP_RECON"  # type: ignore[union-attr]

    # DB: recon_result_task 一行
    task_rows = asyncio.run(
        _query_recon_task_rows(client.app.state.engine, "OCBC")  # type: ignore[union-attr]
    )
    assert len(task_rows) == 1
    row = task_rows[0]
    assert row.status == "NOTIFIED"  # type: ignore[union-attr]
    assert row.file_md5 == "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"  # lower 归一
    assert row.diff_count == 0  # type: ignore[union-attr]
    assert row.task_no == "RECON-OCBC-20260604"  # type: ignore[union-attr]
    assert row.version == 2  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# 2. 同 X-Request-Id 重放 → 200 deduplicated=True，DB 不增行
# ---------------------------------------------------------------------------


def test_same_request_id_dedup(client: TestClient) -> None:
    """相同 X-Request-Id 重放：deduplicated=True；DB inbox/task 各仍 1 行。"""
    r1 = client.post("/api/v1/recon/notify", json=BODY, headers=HEADERS)
    assert r1.status_code == 200
    assert r1.json()["data"]["deduplicated"] is False

    r2 = client.post("/api/v1/recon/notify", json=BODY, headers=HEADERS)
    assert r2.status_code == 200
    assert r2.json()["data"]["deduplicated"] is True

    # DB 不增行
    inbox_rows = asyncio.run(
        _query_inbox_rows(client.app.state.engine, "OCBC", "WEDAP_RECON")  # type: ignore[union-attr]
    )
    assert len(inbox_rows) == 1

    task_rows = asyncio.run(
        _query_recon_task_rows(client.app.state.engine, "OCBC")  # type: ignore[union-attr]
    )
    assert len(task_rows) == 1


# ---------------------------------------------------------------------------
# 3. 坏 X-Request-Id → 400
# ---------------------------------------------------------------------------


def test_bad_request_id_format_400(client: TestClient) -> None:
    """X-Request-Id 非 recon-result-*-v* 格式 → 400 GW_400_VALIDATION。"""
    bad_headers = {**HEADERS, "X-Request-Id": "wrong-format-001"}
    r = client.post("/api/v1/recon/notify", json=BODY, headers=bad_headers)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "GW_400_VALIDATION"


# ---------------------------------------------------------------------------
# 4. 坏 body 参数化 → 400
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_body",
    [
        # 坏 reconDate（非 8 位数字）
        {**BODY, "reconDate": "2026-06-04"},
        # 缺 s3Bucket
        {k: v for k, v in BODY.items() if k != "s3Bucket"},
        # 空 files
        {**BODY, "files": []},
        # files[0] 缺 md5
        {
            **BODY,
            "files": [
                {
                    "fileName": "f.xlsx",
                    "s3Key": "k/f.xlsx",
                    "totalCount": 0,
                    # md5 缺失
                }
            ],
        },
    ],
    ids=["bad_reconDate", "missing_s3Bucket", "empty_files", "missing_md5"],
)
def test_bad_body_400(client: TestClient, bad_body: dict[str, Any]) -> None:
    """各种坏 body → 400 GW_400_VALIDATION。"""
    r = client.post("/api/v1/recon/notify", json=bad_body, headers=HEADERS)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "GW_400_VALIDATION"


# ---------------------------------------------------------------------------
# 5. 缺 X-Tenant-Id → 400；无 caller → 401
# ---------------------------------------------------------------------------


def test_missing_tenant_id_400(client: TestClient) -> None:
    """缺 X-Tenant-Id → 400 GW_400_HEADER。"""
    h = {k: v for k, v in HEADERS.items() if k != "X-Tenant-Id"}
    r = client.post("/api/v1/recon/notify", json=BODY, headers=h)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "GW_400_HEADER"


def test_missing_caller_service_401(client: TestClient) -> None:
    """缺 X-Caller-Service → S2SMiddleware 拦截 → 401。"""
    h = {k: v for k, v in HEADERS.items() if k != "X-Caller-Service"}
    r = client.post("/api/v1/recon/notify", json=BODY, headers=h)
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 6. 非 dedup 的 IntegrityError → 500
# ---------------------------------------------------------------------------


def test_non_dedup_integrity_error_500() -> None:
    """非唯一约束的 IntegrityError（FK/CHECK/NOT NULL 等）→ 500，不被吞为去重。"""
    app = create_app()
    asyncio.run(_create_tables_app(app))

    fake_orig = Exception("FOREIGN KEY constraint failed")
    fake_exc = IntegrityError("statement", {}, fake_orig)

    class _FakeBegin:
        async def __aenter__(self) -> "_FakeBegin":
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            pass

    class _FakeSession:
        async def __aenter__(self) -> "_FakeSession":
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        def begin(self) -> "_FakeBegin":
            return _FakeBegin()

        def add(self, obj: Any) -> None:
            raise fake_exc

    class _FakeFactory:
        def __call__(self) -> "_FakeSession":
            return _FakeSession()

    app.state.session_factory = _FakeFactory()

    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post("/api/v1/recon/notify", json=BODY, headers=HEADERS)
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "GW_500_INTERNAL"


async def _create_tables_app(app: Any) -> None:
    async with app.state.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
