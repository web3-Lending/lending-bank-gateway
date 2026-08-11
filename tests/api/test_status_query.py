"""状态查询 API 测试：GET /status。"""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.clients.wedap import WedapError
from app.main import create_app
from app.models.base import Base

# ── 公共常量 ──────────────────────────────────────────────────────────────────

HEADERS = {
    "X-Caller-Service": "lifecycle",
    "X-Tenant-Id": "OCBC",
    "X-Request-Id": "req-status-1",
    "Idempotency-Key": "CLT-20260611-0001234567890",
}

COLLECT_BODY = {
    "bizSeqNo": "CLT-20260611-0001234567890",
    "txnAmount": "500.0000",
    "currencyCode": "USD",
    "transType": "LOAN_COLLECT",
    # wedap 归集必填集（2026-08-11 实测，app/domain/wedap_contract.COLLECT_REQUIRED）
    "channelId": "LEN",
    "userId": "U1",
    "custAccountNo": "1001234567890",
    "bankAccountName": "TEST COLLECTION ACCOUNT",
    "bankAccountNo": "9999617459809900000215",
    "userList": [{"userId": "U1", "amount": "500.0000"}],
}


async def _seed_pre_0020_order(app) -> None:  # type: ignore[no-untyped-def]
    """直插 0020 前形态存量行（trans_type/ori_req_date 为 NULL）——不走 API：
    新 POST 已强制 transType 必填，无法再产生 NULL 行（codex P2 修法）。"""
    from decimal import Decimal

    from app.models.txn import BankTxnOrder

    async with app.state.session_factory() as s:
        async with s.begin():
            s.add(
                BankTxnOrder(
                    tenant_id="OCBC",
                    biz_seq_no=LEGACY_BIZ,
                    business_action="COLLECT",
                    biz_type="COLL",
                    amount=Decimal("500.0000"),
                    currency="USD",
                    caller_service="lifecycle",
                    status="SUBMITTED",
                )
            )


LEGACY_BIZ = "CLT-20260611-0001234567891"

STATUS_URL = "/api/v1/bank-funds/status"


# ── Fixtures ──────────────────────────────────────────────────────────────────


async def _create_tables(engine) -> None:  # type: ignore[no-untyped-def]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.fixture()
def client() -> TestClient:
    app = create_app()
    asyncio.run(_create_tables(app.state.engine))
    wedap = AsyncMock()
    wedap.collect_from_users.return_value = {"txnStatus": "PROCESSING"}
    wedap.distribute_to_users.return_value = {"txnStatus": "PROCESSING"}
    wedap.query_transaction_status.return_value = {
        "txnStatus": "SUBMITTED",
        "oriBizSeqNo": COLLECT_BODY["bizSeqNo"],
    }
    app.state.wedap = wedap
    return TestClient(app)


# ── Test 1：种单后 status 查询返回 orderStatus + wedap 合成视图 ─────────────────


def test_status_after_submit_returns_order_and_wedap(client: TestClient) -> None:
    """先 POST 种单，GET /status 返回 orderStatus==SUBMITTED + wedap.txnStatus 来自 mock。"""
    # 种单
    client.post("/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS)

    r = client.get(
        STATUS_URL,
        params={"bizSeqNo": COLLECT_BODY["bizSeqNo"]},
        headers=HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    data = body["data"]
    assert data["orderStatus"] == "SUBMITTED"
    assert data["bizSeqNo"] == COLLECT_BODY["bizSeqNo"]
    assert data["wedap"]["txnStatus"] == "SUBMITTED"
    # 验证通用回查供参从 order 取出：trans_type=提交原值，ori_req_date=提交日 YYYYMMDD
    call_kwargs = client.app.state.wedap.query_transaction_status.call_args  # type: ignore[union-attr]
    assert call_kwargs.kwargs["trans_type"] == "LOAN_COLLECT"
    assert len(call_kwargs.kwargs["ori_req_date"]) == 8
    assert call_kwargs.kwargs["ori_biz_seq_no"] == COLLECT_BODY["bizSeqNo"]


# ── Test 2：未知 bizSeqNo → 404 GW_404_ORDER ─────────────────────────────────


def test_status_unknown_biz_seq_no_404(client: TestClient) -> None:
    """未知 bizSeqNo → 404 GW_404_ORDER。"""
    r = client.get(
        STATUS_URL,
        params={"bizSeqNo": "CLT-20260611-NOTEXIST0001"},
        headers=HEADERS,
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "GW_404_ORDER"


# ── Test 3：跨 tenant 隔离 ─────────────────────────────────────────────────────


def test_status_cross_tenant_isolation_404(client: TestClient) -> None:
    """tenant A 种单，tenant B 查 → 404（跨租户不可见）。"""
    # tenant A 种单
    client.post("/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS)

    # tenant B 查询
    headers_b = {**HEADERS, "X-Tenant-Id": "DBS", "X-Request-Id": "req-status-b"}
    r = client.get(
        STATUS_URL,
        params={"bizSeqNo": COLLECT_BODY["bizSeqNo"]},
        headers=headers_b,
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "GW_404_ORDER"


# ── Test 4：wedap 查询超时 → 200 + unavailable=True ──────────────────────────


def test_status_wedap_timeout_degrades_gracefully(client: TestClient) -> None:
    """wedap query_transaction_status 超时 → HTTP 200 且 data.wedap.unavailable is True。"""
    # 种单
    client.post("/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS)

    # 让 wedap 超时
    client.app.state.wedap.query_transaction_status.side_effect = httpx.TimeoutException("timeout")  # type: ignore[union-attr]

    r = client.get(
        STATUS_URL,
        params={"bizSeqNo": COLLECT_BODY["bizSeqNo"]},
        headers=HEADERS,
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["wedap"]["unavailable"] is True
    assert "reason" in data["wedap"]


# ── Test 4b：wedap TransportError 也降级 ─────────────────────────────────────


def test_status_wedap_transport_error_degrades_gracefully(client: TestClient) -> None:
    """wedap query_transaction_status TransportError → HTTP 200 + unavailable=True。"""
    client.post("/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS)
    client.app.state.wedap.query_transaction_status.side_effect = httpx.TransportError("conn reset")  # type: ignore[union-attr]

    r = client.get(
        STATUS_URL,
        params={"bizSeqNo": COLLECT_BODY["bizSeqNo"]},
        headers=HEADERS,
    )
    assert r.status_code == 200
    assert r.json()["data"]["wedap"]["unavailable"] is True


# ── Test 4c：wedap WedapError 也降级 ─────────────────────────────────────────


def test_status_wedap_error_degrades_gracefully(client: TestClient) -> None:
    """wedap query_transaction_status WedapError → HTTP 200 + unavailable=True。"""
    client.post("/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS)
    client.app.state.wedap.query_transaction_status.side_effect = WedapError("500", "internal")  # type: ignore[union-attr]

    r = client.get(
        STATUS_URL,
        params={"bizSeqNo": COLLECT_BODY["bizSeqNo"]},
        headers=HEADERS,
    )
    assert r.status_code == 200
    assert r.json()["data"]["wedap"]["unavailable"] is True


# ── Test 4c2：wedap HTTP 状态错误也降级 ──────────────────────────────────────


def test_status_wedap_http_status_error_degrades_gracefully(client: TestClient) -> None:
    """wedap query_transaction_status HTTPStatusError（非 2xx）→ HTTP 200 + reason=http_error。"""
    client.post("/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS)
    resp = httpx.Response(502, request=httpx.Request("GET", "http://wedap/status"))
    client.app.state.wedap.query_transaction_status.side_effect = httpx.HTTPStatusError(  # type: ignore[union-attr]
        "bad gateway", request=resp.request, response=resp
    )

    r = client.get(
        STATUS_URL,
        params={"bizSeqNo": COLLECT_BODY["bizSeqNo"]},
        headers=HEADERS,
    )
    assert r.status_code == 200
    wedap = r.json()["data"]["wedap"]
    assert wedap["unavailable"] is True
    assert wedap["reason"] == "http_error"


# ── Test 4d：0020 前存量单（trans_type NULL）→ 200 + reason=missing_trans_type ──


def test_status_missing_trans_type_degrades_without_wedap_call(
    client: TestClient,
) -> None:
    """直插 0020 前形态行（trans_type NULL）→ 不打 wedap，reason=missing_trans_type + note。"""
    asyncio.run(_seed_pre_0020_order(client.app))

    r = client.get(
        STATUS_URL,
        params={"bizSeqNo": LEGACY_BIZ},
        headers=HEADERS,
    )
    assert r.status_code == 200
    wedap = r.json()["data"]["wedap"]
    assert wedap["unavailable"] is True
    assert wedap["reason"] == "missing_trans_type"
    # 存量单：补 note 说明以本地 orderStatus 为准，非故障
    assert "orderStatus" in wedap["note"]
    client.app.state.wedap.query_transaction_status.assert_not_called()  # type: ignore[union-attr]


# ── Test 4e：新单缺 transType → 422（必填强校验，禁止再产生 NULL 供参行）─────────


def test_collect_missing_trans_type_rejected_422(client: TestClient) -> None:
    """新 POST 缺 transType → 422（pydantic 必填），不落单不外呼（codex P1 修法）。"""
    body = {k: v for k, v in COLLECT_BODY.items() if k != "transType"}
    headers = {**HEADERS, "Idempotency-Key": body["bizSeqNo"], "X-Request-Id": "req-no-tt"}
    r = client.post("/api/v1/bank-funds/collect-from-users", json=body, headers=headers)
    assert r.status_code == 422
    client.app.state.wedap.collect_from_users.assert_not_called()  # type: ignore[union-attr]


def test_collect_overlong_trans_type_rejected_422(client: TestClient) -> None:
    """transType 超 32 字符 → 422（不静默截断，防回查值 != 提交值；07-24 上限 20→32）。"""
    body = {**COLLECT_BODY, "transType": "X" * 33}
    headers = {**HEADERS, "Idempotency-Key": body["bizSeqNo"], "X-Request-Id": "req-long-tt"}
    r = client.post("/api/v1/bank-funds/collect-from-users", json=body, headers=headers)
    assert r.status_code == 422
    client.app.state.wedap.collect_from_users.assert_not_called()  # type: ignore[union-attr]


def test_collect_official_26char_trans_type_accepted(client: TestClient) -> None:
    """定稿值 BANK_FUND_COLLECT_CLEARING（26 字符，旧限 20 会拒）→ 200 且提交值原样落库回查。"""
    body = {**COLLECT_BODY, "transType": "BANK_FUND_COLLECT_CLEARING"}
    headers = {**HEADERS, "Idempotency-Key": body["bizSeqNo"], "X-Request-Id": "req-official-tt"}
    r = client.post("/api/v1/bank-funds/collect-from-users", json=body, headers=headers)
    assert r.status_code == 200
    sent = client.app.state.wedap.collect_from_users.call_args.kwargs["payload"]  # type: ignore[union-attr]
    assert sent["transType"] == "BANK_FUND_COLLECT_CLEARING"
    st = client.get(STATUS_URL, params={"bizSeqNo": body["bizSeqNo"]}, headers=HEADERS)
    assert st.status_code == 200


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (
            "/api/v1/bank-funds/distribute-to-users",
            {"bizSeqNo": "DST-NOTT-0001", "currencyCode": "USD", "recipients": []},
        ),
        (
            "/api/v1/bank-funds/refunds",
            {
                "bizSeqNo": "RFD-NOTT-0001",
                "currencyCode": "USD",
                "refundAmount": "1.00",
                "oriBizSeqNo": "CLT-X",
            },
        ),
        (
            "/api/v1/loans/p2p-disbursements",
            {
                "bizSeqNo": "DSB-NOTT-0001",
                "disbursementInfo": {"txnAmount": "1.00", "currencyCode": "USD"},
            },
        ),
        (
            "/api/v1/loans/p2p-repayments",
            {
                "bizSeqNo": "RPM-NOTT-0001",
                "repaymentInfo": {"txnAmount": "1.00", "currencyCode": "USD"},
            },
        ),
    ],
)
def test_all_submit_models_missing_trans_type_rejected_422(
    client: TestClient, path: str, body: dict
) -> None:
    """五个提交模型逐个强制 transType 必填——缺失一律 422（collect 见上例，此处盖其余四个）。"""
    headers = {**HEADERS, "Idempotency-Key": body["bizSeqNo"], "X-Request-Id": "req-nt"}
    r = client.post(path, json=body, headers=headers)
    assert r.status_code == 422


# ── Test 5：缺 X-Tenant-Id → 400 ─────────────────────────────────────────────


def test_status_missing_tenant_id_400(client: TestClient) -> None:
    """缺 X-Tenant-Id header → 400 GW_400_HEADER。"""
    h = {k: v for k, v in HEADERS.items() if k != "X-Tenant-Id"}
    r = client.get(
        STATUS_URL,
        params={"bizSeqNo": COLLECT_BODY["bizSeqNo"]},
        headers=h,
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "GW_400_HEADER"
