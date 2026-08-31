"""MONEY_WRITE 写错误的 v2.2 §8.2 typed 字段（`error.details`）。

§8.2 字段表 `resubmitAllowed` 一栏明写「**写错误必须**」，同键异载荷（§9.1 的 422）尤甚：
§9.1 要求调用方「停止重放并先查询原 operation」——那正是 `outcome/retryPolicy/operationId`
要表达的。只给一个错误码字符串，消费方只能回去解析人类文案分支，与本波
「让消费方能按 typed 字段分支」的目的正相反（2026-08-28 独立复核 MAJOR-3）。

三条分界线，本模块逐条钉死：
1. **只有 MONEY_WRITE 端点**才补字段（普通查询 / CRUD 一个字段都不许沾）；
2. **认证错误豁免**（§8.2 明文），401 上不许出现；
3. **dispatch 分界线**——进服务之前的拒绝才敢说 NOT_APPLIED，之后一律 UNKNOWN + 查单地址。
"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models.base import Base

HEADERS = {
    "X-Caller-Service": "lifecycle",
    "X-Tenant-Id": "OCBC",
    "X-Request-Id": "req-1",
}
BODY = {
    "bizSeqNo": "CLT-20260828-0001234567890",
    "transType": "LOAN_COLLECT",
    "txnAmount": "500.0000",
    "currencyCode": "USD",
    "channelId": "LEN",
    "userId": "U1",
    "custAccountNo": "1001234567890",
    "bankAccountNo": "9999617459809900000215",
    "bankAccountName": "TEST COLLECTION ACCOUNT",
    "userList": [{"userId": "U1", "amount": "500.0000"}],
}
_TYPED_KEYS = {"outcome", "operationStatus", "retryPolicy", "resubmitAllowed", "statusUrl"}


async def _create_tables(engine) -> None:  # type: ignore[no-untyped-def]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.fixture()
def client() -> TestClient:
    app = create_app()
    asyncio.run(_create_tables(app.state.engine))
    wedap = AsyncMock()
    wedap.collect_from_users.return_value = {"txnStatus": "PROCESSING"}
    app.state.wedap = wedap
    return TestClient(app)


def _details(response) -> dict:  # type: ignore[no-untyped-def, type-arg]
    return response.json()["error"]["details"]


def test_pre_dispatch_validation_400_says_not_applied(client: TestClient) -> None:
    """报文校验拒绝（未外呼、未建单）→ NOT_APPLIED；**不给查单地址**（没建单=死链）。"""
    r = client.post(
        "/api/v1/bank-funds/collect-from-users",
        json={**BODY, "txnAmount": "-1"},
        headers=HEADERS,
    )
    assert r.status_code == 400
    details = _details(r)
    assert details["outcome"] == "NOT_APPLIED"
    assert details["retryPolicy"] == "CORRECT_AND_NEW_INTENT"
    assert details["resubmitAllowed"] is False
    assert "statusUrl" not in details and "operationId" not in details


def test_missing_required_wedap_field_400_says_not_applied(client: TestClient) -> None:
    """必填集缺失（deps 共享 helper 抛的 400，不在端点函数体内）同样带字段。"""
    body = {k: v for k, v in BODY.items() if k != "bankAccountNo"}
    r = client.post("/api/v1/bank-funds/collect-from-users", json=body, headers=HEADERS)
    assert r.status_code == 400
    assert _details(r)["outcome"] == "NOT_APPLIED"


def test_header_400_on_money_write_still_carries_fields(client: TestClient) -> None:
    """header 校验（路由依赖）也在服务之前：确证零影响。"""
    h = {k: v for k, v in HEADERS.items() if k != "X-Tenant-Id"}
    r = client.post("/api/v1/bank-funds/collect-from-users", json=BODY, headers=h)
    assert r.status_code == 400
    assert _details(r)["outcome"] == "NOT_APPLIED"


def test_schema_validation_422_says_not_applied(client: TestClient) -> None:
    """FastAPI schema 校验（另一个 handler）同样必然在 dispatch 之前。"""
    r = client.post("/api/v1/bank-funds/collect-from-users", json={}, headers=HEADERS)
    assert r.status_code == 422
    details = _details(r)
    assert details["outcome"] == "NOT_APPLIED"
    assert details["errors"]  # 原有字段不被挤掉


def test_bad_biz_seq_no_400_says_not_applied(client: TestClient) -> None:
    """bizSeqNo 格式非法：校验被提到服务调用之前，故能给出确证的 NOT_APPLIED。"""
    r = client.post(
        "/api/v1/bank-funds/collect-from-users",
        json={**BODY, "bizSeqNo": "BAD/KEY?x=1"},
        headers={**HEADERS, "Idempotency-Key": "BAD/KEY?x=1"},
    )
    assert r.status_code == 400
    details = _details(r)
    assert details["outcome"] == "NOT_APPLIED"
    assert "statusUrl" not in details


def test_idempotency_payload_mismatch_422_says_unknown_with_status_url(
    client: TestClient,
) -> None:
    """§9.1：同键不同 payload → 停止重放、先查原 operation。查单地址必须真给。"""
    first = client.post("/api/v1/bank-funds/collect-from-users", json=BODY, headers=HEADERS)
    assert first.status_code == 200
    mutated = {**BODY, "txnAmount": "999.0000", "userList": [{"userId": "U1", "amount": "999"}]}
    r = client.post("/api/v1/bank-funds/collect-from-users", json=mutated, headers=HEADERS)
    assert r.status_code == 422
    details = _details(r)
    assert details["outcome"] == "UNKNOWN"
    assert details["operationStatus"] == "RECONCILING"
    assert details["retryPolicy"] == "POLL_STATUS"
    assert details["resubmitAllowed"] is False
    assert details["operationId"] == BODY["bizSeqNo"]
    assert details["statusUrl"] == f"/api/v1/bank-funds/status?bizSeqNo={BODY['bizSeqNo']}"
    # 查单地址不是死链：原单确实存在（422 的前提就是首单已落库）
    q = client.get(details["statusUrl"], headers=HEADERS)
    assert q.status_code == 200
    assert q.json()["data"]["bizSeqNo"] == BODY["bizSeqNo"]


def test_repayment_payload_mismatch_points_at_dedicated_status_endpoint() -> None:
    """还款的 422 必须指向还款专用查单端点（通用 5.5 查询不返回 debtSettled/steps[]）。"""
    app = create_app()
    asyncio.run(_create_tables(app.state.engine))
    wedap = AsyncMock()
    wedap.submit_repayment.return_value = {"status": "PROCESSING", "debtSettled": False}
    app.state.wedap = wedap
    c = TestClient(app)
    body = {
        "bizSeqNo": "RPMT-20260828-0001234567890",
        "channelId": "LEN",
        "transType": "REPAYMENT",
        "loanNo": "LN20250205000001",
        "repaymentInfo": {
            "txnAmount": "100.0000",
            "currencyCode": "USD",
            "userId": "U1",
            "userName": "u",
            "repaymentType": "SCHEDULED",
            "principalAmount": "80.0000",
            "interestAmount": "20.0000",
        },
        "lenders": [
            {
                "userId": "L1",
                "txnAmount": "100.0000",
                "currencyCode": "USD",
                "principalAmount": "80.0000",
                "interestAmount": "20.0000",
            }
        ],
    }
    assert c.post("/api/v1/loans/p2p-repayments", json=body, headers=HEADERS).status_code == 200
    mutated = {
        **body,
        "repaymentInfo": {**body["repaymentInfo"], "txnAmount": "999.0000"},
        "lenders": [{**body["lenders"][0], "txnAmount": "999.0000"}],
    }
    r = c.post("/api/v1/loans/p2p-repayments", json=mutated, headers=HEADERS)
    assert r.status_code == 422
    assert _details(r)["statusUrl"] == (f"/api/v1/loans/p2p-repayments/{body['bizSeqNo']}/status")


def test_auth_401_carries_no_typed_fields() -> None:
    """§8.2 明文豁免认证错误：401 上一个 typed 字段都不许有。"""
    app = create_app()
    asyncio.run(_create_tables(app.state.engine))
    app.state.wedap = AsyncMock()
    c = TestClient(app)
    r = c.post("/api/v1/bank-funds/collect-from-users", json=BODY, headers={})
    assert r.status_code == 401
    assert not (_TYPED_KEYS & set(_details(r)))


def test_non_money_write_endpoint_4xx_carries_no_typed_fields(client: TestClient) -> None:
    """普通查询的 4xx 一个字段都不许沾（§8.2 禁止为「字段齐全」填无意义默认值）。"""
    r = client.get("/api/v1/bank-funds/status?bizSeqNo=NOPE-404", headers=HEADERS)
    assert r.status_code == 404
    assert not (_TYPED_KEYS & set(_details(r)))


def test_admin_crud_4xx_carries_no_typed_fields(client: TestClient) -> None:
    """普通 CRUD（admin outbox replay 404）同样不得沾——它不走写原语 ack 契约。"""
    r = client.post("/api/v1/admin/outbox/424242/replay", headers=HEADERS)
    assert r.status_code == 404
    assert not (_TYPED_KEYS & set(_details(r)))


def test_post_dispatch_value_error_400_says_unknown_not_not_applied(client: TestClient) -> None:
    """**dispatch 之后**冒出的 400 绝不许说 NOT_APPLIED。

    真实来源：wedap 返 HTTP 200 但响应体不是 JSON（代理塞了 HTML 错误页），
    `WedapClient._unwrap` 里 `r.json()` 抛 `json.JSONDecodeError`——它是 `ValueError`
    子类，会一路冒泡到本 helper 的 `except ValueError` 被包成 400。此时外呼**已经打出去
    了**，若按「400=校验失败=没影响」补 NOT_APPLIED，调用方就会换新 bizSeqNo 重发 =
    重复扣款。故这里只能给 UNKNOWN + 真实查单地址。
    """
    client.app.state.wedap.collect_from_users.side_effect = ValueError(  # type: ignore[union-attr]
        "Expecting value: line 1 column 1 (char 0)"
    )
    r = client.post("/api/v1/bank-funds/collect-from-users", json=BODY, headers=HEADERS)
    assert r.status_code == 400
    details = _details(r)
    assert details["outcome"] == "UNKNOWN"
    assert details["operationStatus"] == "RECONCILING"
    assert details["retryPolicy"] == "POLL_STATUS"
    assert details["statusUrl"] == f"/api/v1/bank-funds/status?bizSeqNo={BODY['bizSeqNo']}"
    # 查单地址真可查：dispatch 前的原子登记已把 order 落库（§9.1 durable operation）
    q = client.get(details["statusUrl"], headers=HEADERS)
    assert q.status_code == 200


def test_reversal_post_dispatch_value_error_400_says_unknown() -> None:
    """冲正端点自带 try/except，与 _submit 同型，同样不许把 dispatch 后的 400 说成零影响。"""
    app = create_app()
    asyncio.run(_create_tables(app.state.engine))
    wedap = AsyncMock()
    wedap.reverse.side_effect = ValueError("Expecting value: line 1 column 1 (char 0)")
    app.state.wedap = wedap
    c = TestClient(app)
    body = {
        "bizSeqNo": "RVSL-20260828-000000001",
        "oriBizSeqNo": "CLT-20260828-000000001",
        "transType": "BANK_FUND_REVERSAL",
        "oriTxnAmount": "500.0000",
        "currencyCode": "USD",
        "channelId": "LEN",
    }
    r = c.post("/api/v1/bank-funds/reversals", json=body, headers=HEADERS)
    assert r.status_code == 400
    details = _details(r)
    assert details["outcome"] == "UNKNOWN"
    assert details["statusUrl"] == f"/api/v1/bank-funds/status?bizSeqNo={body['bizSeqNo']}"


def test_reversal_bad_biz_seq_no_400_says_not_applied() -> None:
    """冲正端点的 dispatch 前格式拒绝：NOT_APPLIED、不给查单地址。"""
    app = create_app()
    asyncio.run(_create_tables(app.state.engine))
    app.state.wedap = AsyncMock()
    c = TestClient(app)
    body = {
        "bizSeqNo": "RVSL/BAD?x=1",
        "oriBizSeqNo": "CLT-20260828-000000002",
        "transType": "BANK_FUND_REVERSAL",
        "oriTxnAmount": "500.0000",
        "currencyCode": "USD",
        "channelId": "LEN",
    }
    r = c.post("/api/v1/bank-funds/reversals", json=body, headers=HEADERS)
    assert r.status_code == 400
    details = _details(r)
    assert details["outcome"] == "NOT_APPLIED"
    assert "statusUrl" not in details


def test_loans_post_dispatch_value_error_400_says_unknown() -> None:
    """放款端点（loans._submit）同型：dispatch 后的 400 只能给 UNKNOWN + 查单地址。"""
    app = create_app()
    asyncio.run(_create_tables(app.state.engine))
    wedap = AsyncMock()
    wedap.submit_disbursement.side_effect = ValueError("Expecting value: line 1 column 1 (char 0)")
    app.state.wedap = wedap
    c = TestClient(app)
    body = {
        "bizSeqNo": "DSB-20260828-0001234567890",
        "channelId": "LEN",
        "transType": "DISBURSEMENT",
        "disbursementInfo": {
            "txnAmount": "100.0000",
            "currencyCode": "USD",
            "userId": "U1",
            "userName": "u",
        },
        "lenders": [{"userId": "L1", "lendAmount": "100.0000", "currencyCode": "USD"}],
    }
    r = c.post("/api/v1/loans/p2p-disbursements", json=body, headers=HEADERS)
    assert r.status_code == 400
    details = _details(r)
    assert details["outcome"] == "UNKNOWN"
    assert details["statusUrl"] == f"/api/v1/bank-funds/status?bizSeqNo={body['bizSeqNo']}"
