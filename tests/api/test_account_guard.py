"""账户守门人测试：collect/distribute/refund 平台账户白名单校验（金融级）。

覆盖 §8 用例矩阵：off/observe/enforce 三态、reject 四细分原因、fail-closed
断言（wedap 零调用 + 不落 order + 不占幂等）、per-tenant 隔离、空白 strip、
审计落库、refund 端点同样收口。
"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.config import Settings
from app.main import create_app
from app.models.audit import AuditLog
from app.models.base import Base
from app.models.platform_account import PlatformBankAccount
from app.models.txn import BankTxnOrder

TENANT = "OCBC"
ESCROW = "ESCROW-USD-001"

HEADERS = {
    "X-Caller-Service": "lifecycle",
    "X-Tenant-Id": TENANT,
    "X-Request-Id": "req-1",
    "Idempotency-Key": "CLT-20260716-0001234567890",
}
COLLECT_BODY = {
    "bizSeqNo": "CLT-20260716-0001234567890",
    "transType": "LOAN_COLLECT",
    "totalAmount": "500.0000",
    "currencyCode": "USD",
    "bankAccountNo": ESCROW,
    "userList": [{"userId": "U1", "amount": "500.0000"}],
}
DISTRIBUTE_HEADERS = HEADERS | {"Idempotency-Key": "DST-20260716-0001234567890"}
DISTRIBUTE_BODY = {
    "bizSeqNo": "DST-20260716-0001234567890",
    "transType": "BANK_FUND_DISTRIBUTE",
    "currencyCode": "USD",
    "bankAccountNo": ESCROW,
    "recipients": [{"userId": "U2", "distributeAmount": "200.0000", "currencyCode": "USD"}],
}
REFUND_HEADERS = HEADERS | {"Idempotency-Key": "RFD-20260716-0001234567890"}
REFUND_BODY = {
    "bizSeqNo": "RFD-20260716-0001234567890",
    "transType": "LOAN_REFUND",
    "currencyCode": "USD",
    "refundAmount": "10.0000",
    "oriBizSeqNo": "CLT-20260716-0001234567890",
    "bankAccountNo": ESCROW,
}


async def _create_tables(engine) -> None:  # type: ignore[no-untyped-def]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _make_client(mode: str) -> TestClient:
    app = create_app(settings=Settings(account_guard_mode=mode))
    asyncio.run(_create_tables(app.state.engine))
    wedap = AsyncMock()
    wedap.collect_from_users.return_value = {"txnStatus": "PROCESSING"}
    wedap.distribute_to_users.return_value = {"txnStatus": "PROCESSING"}
    wedap.refund.return_value = {"txnStatus": "PROCESSING"}
    app.state.wedap = wedap
    return TestClient(app)


def _seed_account(app, **overrides) -> None:  # type: ignore[no-untyped-def]
    row = dict(
        tenant_id=TENANT,
        account_no=ESCROW,
        purpose="escrow",
        allowed_scopes="bank_collect,bank_distribute,bank_refund",
        currency=None,
        status="active",
    )
    row.update(overrides)

    async def _insert() -> None:
        async with app.state.session_factory() as session:
            async with session.begin():
                session.add(PlatformBankAccount(**row))

    asyncio.run(_insert())


def _count(app, model) -> int:  # type: ignore[no-untyped-def]
    async def _q() -> int:
        async with app.state.session_factory() as session:
            return (await session.execute(select(func.count()).select_from(model))).scalar_one()

    return asyncio.run(_q())


def _audit_actions(app) -> list[str]:  # type: ignore[no-untyped-def]
    async def _q() -> list[str]:
        async with app.state.session_factory() as session:
            return list((await session.execute(select(AuditLog.action))).scalars().all())

    return asyncio.run(_q())


@pytest.fixture()
def enforce_client() -> TestClient:
    return _make_client("enforce")


# ── off：默认零行为变更 ────────────────────────────────────────────────────────


def test_off_mode_no_whitelist_still_allows() -> None:
    client = _make_client("off")
    r = client.post("/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS)
    assert r.status_code == 200
    assert client.app.state.wedap.collect_from_users.await_count == 1  # type: ignore[union-attr]


# ── enforce：放行面 ───────────────────────────────────────────────────────────


def test_enforce_registered_active_scope_hit_allows(enforce_client: TestClient) -> None:
    _seed_account(enforce_client.app)
    r = enforce_client.post(
        "/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS
    )
    assert r.status_code == 200
    r2 = enforce_client.post(
        "/api/v1/bank-funds/distribute-to-users", json=DISTRIBUTE_BODY, headers=DISTRIBUTE_HEADERS
    )
    assert r2.status_code == 200


def test_enforce_currency_unrestricted_allows_any(enforce_client: TestClient) -> None:
    _seed_account(enforce_client.app, currency=None)
    r = enforce_client.post(
        "/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS
    )
    assert r.status_code == 200


def test_enforce_whitespace_stripped_matches(enforce_client: TestClient) -> None:
    _seed_account(enforce_client.app)
    body = COLLECT_BODY | {"bankAccountNo": f"  {ESCROW}  "}
    r = enforce_client.post("/api/v1/bank-funds/collect-from-users", json=body, headers=HEADERS)
    assert r.status_code == 200


# ── enforce：reject 四态 + fail-closed 断言 ───────────────────────────────────


def _assert_rejected(client: TestClient, resp, reason: str) -> None:  # type: ignore[no-untyped-def]
    assert resp.status_code == 403
    err = resp.json()["error"]
    assert err["code"] == "GW_403_ACCOUNT_NOT_ALLOWED"
    assert err["details"]["reason"] == reason
    # fail-closed：不调 wedap、不落 order、审计已记 reject
    wedap = client.app.state.wedap
    assert wedap.collect_from_users.await_count == 0  # type: ignore[union-attr]
    assert wedap.distribute_to_users.await_count == 0  # type: ignore[union-attr]
    assert wedap.refund.await_count == 0  # type: ignore[union-attr]
    assert _count(client.app, BankTxnOrder) == 0
    assert "account_guard.reject" in _audit_actions(client.app)


def test_enforce_unregistered_rejected_empty_whitelist_fail_closed(
    enforce_client: TestClient,
) -> None:
    """D1 fail-closed：租户名单为空 = 配置缺失 ≠ 豁免，一律拒。"""
    r = enforce_client.post(
        "/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS
    )
    _assert_rejected(enforce_client, r, "account_not_registered")


def test_enforce_disabled_rejected(enforce_client: TestClient) -> None:
    _seed_account(enforce_client.app, status="disabled")
    r = enforce_client.post(
        "/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS
    )
    _assert_rejected(enforce_client, r, "account_disabled")


def test_enforce_scope_not_allowed_rejected(enforce_client: TestClient) -> None:
    """escrow 户只许 collect：拿去 distribute → scope_not_allowed。"""
    _seed_account(enforce_client.app, allowed_scopes="bank_collect")
    r = enforce_client.post(
        "/api/v1/bank-funds/distribute-to-users", json=DISTRIBUTE_BODY, headers=DISTRIBUTE_HEADERS
    )
    _assert_rejected(enforce_client, r, "scope_not_allowed")


def test_enforce_currency_mismatch_rejected(enforce_client: TestClient) -> None:
    """D4：白名单限定 HKD 的户走 USD 归集 → currency_mismatch。"""
    _seed_account(enforce_client.app, currency="HKD")
    r = enforce_client.post(
        "/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS
    )
    _assert_rejected(enforce_client, r, "currency_mismatch")


def test_enforce_missing_account_no_rejected(enforce_client: TestClient) -> None:
    _seed_account(enforce_client.app)
    body = {k: v for k, v in COLLECT_BODY.items() if k != "bankAccountNo"}
    r = enforce_client.post("/api/v1/bank-funds/collect-from-users", json=body, headers=HEADERS)
    _assert_rejected(enforce_client, r, "account_missing")


def test_enforce_tenant_isolation(enforce_client: TestClient) -> None:
    """tenant A 登记的账户，tenant B 请求视为未登记。"""
    _seed_account(enforce_client.app, tenant_id="OTHER_TENANT")
    r = enforce_client.post(
        "/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS
    )
    _assert_rejected(enforce_client, r, "account_not_registered")


def test_enforce_refund_endpoint_guarded(enforce_client: TestClient) -> None:
    """refund（07-14 新增端点，方案外评审补入）同样收口。"""
    r = enforce_client.post("/api/v1/bank-funds/refunds", json=REFUND_BODY, headers=REFUND_HEADERS)
    _assert_rejected(enforce_client, r, "account_not_registered")


def test_enforce_reject_does_not_burn_idempotency_key(enforce_client: TestClient) -> None:
    """被拒请求不占幂等：补登白名单后同 key 重放应放行成功。"""
    r1 = enforce_client.post(
        "/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS
    )
    assert r1.status_code == 403
    _seed_account(enforce_client.app)
    r2 = enforce_client.post(
        "/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS
    )
    assert r2.status_code == 200
    assert enforce_client.app.state.wedap.collect_from_users.await_count == 1  # type: ignore[union-attr]


# ── observe：不拒只记 ─────────────────────────────────────────────────────────


def test_observe_mode_allows_but_records() -> None:
    client = _make_client("observe")
    r = client.post("/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS)
    assert r.status_code == 200
    assert client.app.state.wedap.collect_from_users.await_count == 1  # type: ignore[union-attr]
    assert "account_guard.observe" in _audit_actions(client.app)


def test_observe_mode_registered_account_no_audit_noise() -> None:
    """observe 下合法账户不产生守门审计行（只记「会被拒」项）。"""
    client = _make_client("observe")
    _seed_account(client.app)
    r = client.post("/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS)
    assert r.status_code == 200
    assert not [a for a in _audit_actions(client.app) if a.startswith("account_guard.")]


# ── config 校验 ───────────────────────────────────────────────────────────────


def test_invalid_guard_mode_fail_fast() -> None:
    with pytest.raises(ValueError, match="GW_ACCOUNT_GUARD_MODE"):
        Settings(account_guard_mode="observ")


def test_guard_mode_case_insensitive() -> None:
    assert Settings(account_guard_mode="ENFORCE").account_guard_mode == "enforce"
