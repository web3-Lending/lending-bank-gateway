"""deposit/users 查询透传 API 测试。

覆盖：
1. balances/total 透传 + QueryAudit 落 1 行 + BalanceSnapshot 按 accounts 落行（字段断言）
2. accounts 透传 + 审计落行、无快照
3. users/info 任意 query 参数透传（params 原样进 wedap call——断言 mock 调用参数）
4. wedap 超时 → 502 GW_502_UPSTREAM；WedapError → 502
5. 脏余额数据（balance="abc"）→ 200 + 快照跳过该行 + warning
6. 缺 X-Tenant-Id → 400
7. 幂等语义：GET 重复调用审计行累加（查询审计非去重）断言 2 行
8. T22 缺 custAccountNo（None）→ 不落快照 + warning
"""

import asyncio
import logging
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.clients.wedap import WedapError
from app.main import create_app
from app.models.base import Base
from app.models.query_audit import BalanceSnapshot, QueryAudit

HEADERS = {
    "X-Caller-Service": "lifecycle",
    "X-Tenant-Id": "OCBC",
    "X-Request-Id": "req-deposit-1",
}

BALANCE_DATA = {
    "accounts": [
        {"custAccountNo": "ACC001", "balance": "1000.5000", "currencyCode": "USD"},
        {"custAccountNo": "ACC002", "balance": "500.0000", "currencyCode": "HKD"},
    ]
}

ACCOUNTS_DATA = {
    "accounts": [
        {"custAccountNo": "ACC001", "accountType": "SAVINGS"},
    ]
}

USER_INFO_DATA = {
    "userId": "U1",
    "userName": "Test User",
    "email": "test@example.com",
}


async def _create_tables(engine) -> None:  # type: ignore[no-untyped-def]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _count_audit_rows(session_factory, *, tenant_id: str, endpoint: str) -> int:
    async with session_factory() as session:
        result = await session.execute(
            select(func.count()).where(
                QueryAudit.tenant_id == tenant_id,
                QueryAudit.endpoint == endpoint,
            )
        )
        return result.scalar_one()


async def _get_snapshots(session_factory, *, tenant_id: str) -> list[BalanceSnapshot]:
    async with session_factory() as session:
        result = await session.execute(
            select(BalanceSnapshot).where(BalanceSnapshot.tenant_id == tenant_id)
        )
        return list(result.scalars().all())


DETAIL_DATA = {
    "custAccountNo": "12348401030101002088",
    "accountName": "VH测试个人客户",
    "currencyCode": "USD",
    "accountBalance": "10004.0000",
    "availableBalance": "10004.0000",
    "accountStatus": "ACTIVE",
}


@pytest.fixture()
def client() -> TestClient:
    app = create_app()
    asyncio.run(_create_tables(app.state.engine))
    wedap = AsyncMock()
    wedap.get_deposit_balance_total.return_value = BALANCE_DATA
    wedap.get_deposit_accounts.return_value = ACCOUNTS_DATA
    wedap.get_user_info.return_value = USER_INFO_DATA
    wedap.get_deposit_account_detail.return_value = DETAIL_DATA
    app.state.wedap = wedap
    return TestClient(app)


# ── 1. balances/total 透传 + QueryAudit 1 行 + BalanceSnapshot 按 accounts 落行 ──


def test_balance_total_passthrough_and_audit(client: TestClient) -> None:
    r = client.get("/api/v1/deposit/balances/total", params={"userId": "U1"}, headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["data"] == BALANCE_DATA
    assert body["trace_id"]

    sf = client.app.state.session_factory  # type: ignore[union-attr]

    # QueryAudit 落 1 行
    count = asyncio.run(_count_audit_rows(sf, tenant_id="OCBC", endpoint="deposit/balances/total"))
    assert count == 1

    # BalanceSnapshot 落 2 行（accounts 有 2 条）
    snapshots = asyncio.run(_get_snapshots(sf, tenant_id="OCBC"))
    assert len(snapshots) == 2

    snap_map = {s.account_id: s for s in snapshots}

    # ACC001 字段断言
    s1 = snap_map["ACC001"]
    assert s1.balance == Decimal("1000.5000")
    assert s1.currency == "USD"
    assert s1.source_endpoint == "deposit/balances/total"
    assert s1.captured_at is not None

    # ACC002 字段断言
    s2 = snap_map["ACC002"]
    assert s2.balance == Decimal("500.0000")
    assert s2.currency == "HKD"


# ── 2. accounts 透传 + 审计落行、无快照 ──


def test_accounts_passthrough_audit_no_snapshot(client: TestClient) -> None:
    r = client.get("/api/v1/deposit/accounts", params={"userId": "U1"}, headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["data"] == ACCOUNTS_DATA

    sf = client.app.state.session_factory  # type: ignore[union-attr]

    count = asyncio.run(_count_audit_rows(sf, tenant_id="OCBC", endpoint="deposit/accounts"))
    assert count == 1

    # accounts 端点不写快照
    snapshots = asyncio.run(_get_snapshots(sf, tenant_id="OCBC"))
    assert len(snapshots) == 0


# ── 2b. balances/accounts 全透传 query params（含 wedap 必填 bizSeqNo/channelId）──


def test_balance_total_forwards_all_query_params(client: TestClient) -> None:
    """契约 C 薄透传：balances/total 把 bizSeqNo/channelId 等全部 query params 转发 wedap。"""
    r = client.get(
        "/api/v1/deposit/balances/total",
        params={"userId": "U1", "bizSeqNo": "BAL-001", "channelId": "LEN"},
        headers=HEADERS,
    )
    assert r.status_code == 200
    params = client.app.state.wedap.get_deposit_balance_total.call_args.kwargs["params"]  # type: ignore[union-attr]
    assert params["userId"] == "U1"
    assert params["bizSeqNo"] == "BAL-001"
    assert params["channelId"] == "LEN"


def test_accounts_forwards_all_query_params(client: TestClient) -> None:
    """accounts 同样全透传 bizSeqNo/channelId（修复前只转 userId）。"""
    r = client.get(
        "/api/v1/deposit/accounts",
        params={"userId": "U1", "bizSeqNo": "ACC-001", "channelId": "LEN"},
        headers=HEADERS,
    )
    assert r.status_code == 200
    params = client.app.state.wedap.get_deposit_accounts.call_args.kwargs["params"]  # type: ignore[union-attr]
    assert params["bizSeqNo"] == "ACC-001" and params["channelId"] == "LEN"


# ── 3. users/info 任意 query 参数透传 ──


def test_users_info_params_passthrough(client: TestClient) -> None:
    custom_params = {"userId": "U1", "idType": "PASSPORT", "idNo": "P12345"}
    r = client.get("/api/v1/users/info", params=custom_params, headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["data"] == USER_INFO_DATA

    # 断言 wedap mock 被调用时 params 原样传入
    wedap = client.app.state.wedap  # type: ignore[union-attr]
    wedap.get_user_info.assert_awaited_once()
    call_kwargs = wedap.get_user_info.await_args.kwargs
    assert call_kwargs["params"] == custom_params

    sf = client.app.state.session_factory  # type: ignore[union-attr]
    count = asyncio.run(_count_audit_rows(sf, tenant_id="OCBC", endpoint="users/info"))
    assert count == 1


# ── 4. wedap 超时 → 502 GW_502_UPSTREAM；WedapError → 502 ──


def test_timeout_returns_502(client: TestClient) -> None:
    client.app.state.wedap.get_deposit_balance_total.side_effect = httpx.ReadTimeout(  # type: ignore[union-attr]
        "timeout"
    )
    r = client.get("/api/v1/deposit/balances/total", params={"userId": "U1"}, headers=HEADERS)
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "GW_502_UPSTREAM"
    assert "unreachable" in r.json()["error"]["message"]


def test_wedap_error_returns_502(client: TestClient) -> None:
    client.app.state.wedap.get_deposit_balance_total.side_effect = WedapError("500", "internal")  # type: ignore[union-attr]
    r = client.get("/api/v1/deposit/balances/total", params={"userId": "U1"}, headers=HEADERS)
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "GW_502_UPSTREAM"
    # message 含 wedap code，不含 msg 细节
    assert "wedap code 500" in r.json()["error"]["message"]
    assert "internal" not in r.json()["error"]["message"]


# ── 5. 脏余额数据（balance="abc"）→ 200 + 快照跳过该行 + warning ──


def test_dirty_balance_skips_snapshot_and_warns(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    dirty_data = {
        "accounts": [
            {"custAccountNo": "ACC_DIRTY", "balance": "abc", "currencyCode": "USD"},
            {"custAccountNo": "ACC_OK", "balance": "200.0000", "currencyCode": "USD"},
        ]
    }
    client.app.state.wedap.get_deposit_balance_total.return_value = dirty_data  # type: ignore[union-attr]

    with caplog.at_level(logging.WARNING, logger="app.api.v1.deposit"):
        r = client.get("/api/v1/deposit/balances/total", params={"userId": "U1"}, headers=HEADERS)

    assert r.status_code == 200
    assert r.json()["success"] is True

    # warning 日志被触发
    assert any("invalid balance" in rec.message for rec in caplog.records)

    sf = client.app.state.session_factory  # type: ignore[union-attr]
    snapshots = asyncio.run(_get_snapshots(sf, tenant_id="OCBC"))
    # 只有 ACC_OK 成功写入，ACC_DIRTY 被跳过
    assert len(snapshots) == 1
    assert snapshots[0].account_id == "ACC_OK"
    assert snapshots[0].balance == Decimal("200.0000")


# ── 6. 缺 X-Tenant-Id → 400 ──


def test_missing_tenant_id_returns_400(client: TestClient) -> None:
    h = {k: v for k, v in HEADERS.items() if k != "X-Tenant-Id"}
    r = client.get("/api/v1/deposit/balances/total", params={"userId": "U1"}, headers=h)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "GW_400_HEADER"


def test_missing_tenant_id_accounts_400(client: TestClient) -> None:
    h = {k: v for k, v in HEADERS.items() if k != "X-Tenant-Id"}
    r = client.get("/api/v1/deposit/accounts", params={"userId": "U1"}, headers=h)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "GW_400_HEADER"


def test_missing_tenant_id_users_info_400(client: TestClient) -> None:
    h = {k: v for k, v in HEADERS.items() if k != "X-Tenant-Id"}
    r = client.get("/api/v1/users/info", params={"userId": "U1"}, headers=h)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "GW_400_HEADER"


# ── 7. 幂等语义：GET 重复调用审计行累加（查询审计非去重）→ 2 行 ──


def test_repeated_get_accumulates_audit_rows(client: TestClient) -> None:
    """查询审计是累加的（非幂等去重），同 userId 两次 GET → 2 行审计。"""
    client.get("/api/v1/deposit/accounts", params={"userId": "U1"}, headers=HEADERS)
    client.get("/api/v1/deposit/accounts", params={"userId": "U1"}, headers=HEADERS)

    sf = client.app.state.session_factory  # type: ignore[union-attr]
    count = asyncio.run(_count_audit_rows(sf, tenant_id="OCBC", endpoint="deposit/accounts"))
    assert count == 2


# ── 8. T22 缺 custAccountNo（None）→ 不落快照 + warning ──


def test_missing_cust_account_no_skips_snapshot_and_warns(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """accounts 中 custAccountNo 缺失（None）→ 跳过该行快照 + warning；正常行仍落快照。"""
    data_with_null = {
        "accounts": [
            {"custAccountNo": None, "balance": "300.0000", "currencyCode": "USD"},
            {"custAccountNo": "ACC_GOOD", "balance": "150.0000", "currencyCode": "HKD"},
        ]
    }
    client.app.state.wedap.get_deposit_balance_total.return_value = data_with_null  # type: ignore[union-attr]

    with caplog.at_level(logging.WARNING, logger="app.api.v1.deposit"):
        r = client.get("/api/v1/deposit/balances/total", params={"userId": "U1"}, headers=HEADERS)

    assert r.status_code == 200
    assert r.json()["success"] is True

    # warning 日志被触发（missing custAccountNo）
    assert any("missing custAccountNo" in rec.message for rec in caplog.records)

    sf = client.app.state.session_factory  # type: ignore[union-attr]
    snapshots = asyncio.run(_get_snapshots(sf, tenant_id="OCBC"))
    # 只有 ACC_GOOD 成功写入，None custAccountNo 的行被跳过
    assert len(snapshots) == 1
    assert snapshots[0].account_id == "ACC_GOOD"
    assert snapshots[0].balance == Decimal("150.0000")


# ── 5.3 account/detail 透传（§5.3 新代理端点）──────────────────────────────────


def test_account_detail_passthrough_and_audit(client: TestClient) -> None:
    """账户详情：契约 C 全透传（custAccountNo/subaccountSerialNo 等）+ QueryAudit 1 行。"""
    r = client.get(
        "/api/v1/deposit/account/detail",
        params={
            "custAccountNo": "12348401030101002088",
            "subaccountSerialNo": "01",
            "bizSeqNo": "Q-DTL-01",
            "channelId": "LEN",
        },
        headers=HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["data"] == DETAIL_DATA
    call = client.app.state.wedap.get_deposit_account_detail.call_args  # type: ignore[union-attr]
    assert call.kwargs["params"]["custAccountNo"] == "12348401030101002088"
    assert call.kwargs["params"]["bizSeqNo"] == "Q-DTL-01"
    sf = client.app.state.session_factory  # type: ignore[union-attr]
    count = asyncio.run(_count_audit_rows(sf, tenant_id="OCBC", endpoint="deposit/account/detail"))
    assert count == 1


def test_account_detail_upstream_error_maps_502(client: TestClient) -> None:
    """wedap 业务错误（如 D-5 Account not found 的 1001C…）→ 502 GW_502_UPSTREAM。"""
    client.app.state.wedap.get_deposit_account_detail.side_effect = WedapError(  # type: ignore[union-attr]
        "1001C00000001", "Account not found"
    )
    r = client.get(
        "/api/v1/deposit/account/detail",
        params={"custAccountNo": "X", "bizSeqNo": "Q-DTL-02", "channelId": "LEN"},
        headers=HEADERS,
    )
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "GW_502_UPSTREAM"
