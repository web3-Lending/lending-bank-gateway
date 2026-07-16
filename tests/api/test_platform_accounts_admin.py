"""平台账户白名单 admin 端点测试：GW_ADMIN_CALLERS fail-closed + CRUD + 审计。"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import Settings
from app.main import create_app
from app.models.audit import AuditLog
from app.models.base import Base

TENANT = "OCBC"
OPS_HEADERS = {
    "X-Caller-Service": "fund-ops",
    "X-Tenant-Id": TENANT,
    "X-Request-Id": "req-admin-1",
}
CREATE_BODY = {
    "tenantId": TENANT,
    "accountNo": "ESCROW-USD-001",
    "purpose": "escrow",
    "allowedScopes": "bank_collect,bank_distribute",
    "status": "active",
    "note": "P2P 中转户",
}


async def _create_tables(engine) -> None:  # type: ignore[no-untyped-def]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _make_client(admin_callers: str = "fund-ops") -> TestClient:
    app = create_app(settings=Settings(admin_callers=admin_callers))
    asyncio.run(_create_tables(app.state.engine))
    app.state.wedap = AsyncMock()
    return TestClient(app)


@pytest.fixture()
def client() -> TestClient:
    return _make_client()


def _audit_actions(app) -> list[str]:  # type: ignore[no-untyped-def]
    async def _q() -> list[str]:
        async with app.state.session_factory() as session:
            return list((await session.execute(select(AuditLog.action))).scalars().all())

    return asyncio.run(_q())


# ── caller 门禁：空 = fail-closed ─────────────────────────────────────────────


def test_empty_admin_callers_rejects_everything() -> None:
    client = _make_client(admin_callers="")
    for method, url, kwargs in [
        ("get", f"/api/v1/admin/platform-accounts?tenantId={TENANT}", {}),
        ("post", "/api/v1/admin/platform-accounts", {"json": CREATE_BODY}),
        ("patch", "/api/v1/admin/platform-accounts/1", {"json": {"status": "disabled"}}),
    ]:
        r = getattr(client, method)(url, headers=OPS_HEADERS, **kwargs)
        assert r.status_code == 403, f"{method} {url} should be fail-closed"
        assert r.json()["error"]["code"] == "GW_403_ADMIN_CALLER"


def test_caller_not_in_whitelist_rejected(client: TestClient) -> None:
    headers = OPS_HEADERS | {"X-Caller-Service": "lifecycle"}
    r = client.post("/api/v1/admin/platform-accounts", json=CREATE_BODY, headers=headers)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "GW_403_ADMIN_CALLER"


# ── CRUD 正常路径 ─────────────────────────────────────────────────────────────


def test_create_list_patch_roundtrip(client: TestClient) -> None:
    r = client.post("/api/v1/admin/platform-accounts", json=CREATE_BODY, headers=OPS_HEADERS)
    assert r.status_code == 200
    created = r.json()["data"]
    assert created["accountNo"] == "ESCROW-USD-001"
    assert created["status"] == "active"

    r2 = client.get(f"/api/v1/admin/platform-accounts?tenantId={TENANT}", headers=OPS_HEADERS)
    assert r2.status_code == 200
    assert r2.json()["data"]["count"] == 1

    r3 = client.patch(
        f"/api/v1/admin/platform-accounts/{created['id']}",
        json={"status": "disabled", "note": "临时停用"},
        headers=OPS_HEADERS,
    )
    assert r3.status_code == 200
    assert r3.json()["data"]["status"] == "disabled"
    assert r3.json()["data"]["note"] == "临时停用"

    actions = _audit_actions(client.app)
    assert actions.count("platform_account.create") == 1
    assert actions.count("platform_account.update") == 1


def test_create_duplicate_409(client: TestClient) -> None:
    assert (
        client.post(
            "/api/v1/admin/platform-accounts", json=CREATE_BODY, headers=OPS_HEADERS
        ).status_code
        == 200
    )
    r = client.post("/api/v1/admin/platform-accounts", json=CREATE_BODY, headers=OPS_HEADERS)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "GW_409_DUPLICATE"


def test_patch_missing_row_404(client: TestClient) -> None:
    r = client.patch(
        "/api/v1/admin/platform-accounts/9999",
        json={"status": "disabled"},
        headers=OPS_HEADERS,
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "GW_404_PLATFORM_ACCOUNT"


# ── 入参校验 ──────────────────────────────────────────────────────────────────


def test_create_invalid_status_400(client: TestClient) -> None:
    r = client.post(
        "/api/v1/admin/platform-accounts",
        json=CREATE_BODY | {"status": "frozen"},
        headers=OPS_HEADERS,
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "GW_400_VALIDATION"


def test_create_invalid_scopes_csv_400(client: TestClient) -> None:
    r = client.post(
        "/api/v1/admin/platform-accounts",
        json=CREATE_BODY | {"allowedScopes": "bank_collect,,BAD SCOPE"},
        headers=OPS_HEADERS,
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "GW_400_VALIDATION"


def test_patch_scopes_takes_effect_for_guard(client: TestClient) -> None:
    """端到端：登记→enforce 放行；PATCH 摘掉 scope→同请求被拒（改表即改行为）。"""
    from tests.api.test_account_guard import COLLECT_BODY, HEADERS

    app = create_app(settings=Settings(account_guard_mode="enforce", admin_callers="fund-ops"))
    asyncio.run(_create_tables(app.state.engine))
    wedap = AsyncMock()
    wedap.collect_from_users.return_value = {"txnStatus": "PROCESSING"}
    app.state.wedap = wedap
    c = TestClient(app)

    created = c.post(
        "/api/v1/admin/platform-accounts", json=CREATE_BODY, headers=OPS_HEADERS
    ).json()["data"]
    assert (
        c.post(
            "/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS
        ).status_code
        == 200
    )
    c.patch(
        f"/api/v1/admin/platform-accounts/{created['id']}",
        json={"allowedScopes": "bank_distribute"},
        headers=OPS_HEADERS,
    )
    r = c.post(
        "/api/v1/bank-funds/collect-from-users",
        json=COLLECT_BODY | {"bizSeqNo": "CLT-20260716-0009999999999"},
        headers=HEADERS | {"Idempotency-Key": "CLT-20260716-0009999999999"},
    )
    assert r.status_code == 403
    assert r.json()["error"]["details"]["reason"] == "scope_not_allowed"
