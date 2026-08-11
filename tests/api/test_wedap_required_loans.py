"""loans 域（放款 / 还款）的 wedap 必填集入口校验（2026-08-11 实测契约）。

来由：codex 复核指出 bank-funds 三端点已挡、loans 域仍会「gateway 放行 → wedap 400 拒」，
而 gateway 在外呼前已落 ACCEPTED 单（`app/services/submit.py`），被拒后翻 FAILED ——
每条纯字段错误都白留一条垃圾单在 bank_txn_order 资金台账里。

基线报文对齐 2026-08-11 实测通过 wedap schema 校验的字段集；断言只对照
`app.domain.wedap_contract` 契约真源，不写死字面量。
"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.domain.wedap_contract import (
    DISBURSEMENT_INFO_REQUIRED,
    DISBURSEMENT_LENDER_REQUIRED,
    DISBURSEMENT_REQUIRED,
    REPAYMENT_INFO_REQUIRED,
    REPAYMENT_LENDER_REQUIRED,
    REPAYMENT_REQUIRED,
)
from app.main import create_app
from app.models.base import Base

HEADERS = {
    "X-Caller-Service": "lifecycle",
    "X-Tenant-Id": "OCBC",
    "X-Request-Id": "req-loans-1",
}

REAL_DISBURSE = {
    "bizSeqNo": "DSB-20260811-0000000000001",
    "transType": "DISBURSEMENT",
    "channelId": "LEN",
    "disbursementInfo": {
        "txnAmount": "1.0000",
        "currencyCode": "USD",
        "userId": "09966101774108",
        "userName": "TEST BORROWER",
    },
    "lenders": [
        {
            "userId": "09966101774109",
            "userName": "TEST LENDER",
            "currencyCode": "USD",
            "lendAmount": "1.0000",
        }
    ],
}

REAL_REPAY = {
    "bizSeqNo": "RPM-20260811-0000000000001",
    "transType": "REPAYMENT",
    "channelId": "LEN",
    "repaymentInfo": {
        "txnAmount": "1.0000",
        "currencyCode": "USD",
        "userId": "09966101774108",
        "userName": "TEST BORROWER",
        "repaymentType": "SCHEDULED",
        "principalAmount": "0.8000",
        "interestAmount": "0.2000",
    },
    "lenders": [
        {
            "userId": "09966101774109",
            "userName": "TEST LENDER",
            "currencyCode": "USD",
            "txnAmount": "1.0000",
            "principalAmount": "0.8000",
            "interestAmount": "0.2000",
        }
    ],
}


async def _create_tables(engine) -> None:  # type: ignore[no-untyped-def]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.fixture()
def client() -> TestClient:
    app = create_app()
    asyncio.run(_create_tables(app.state.engine))
    wedap = AsyncMock()
    wedap.p2p_disbursement.return_value = {"txnStatus": "PROCESSING"}
    wedap.p2p_repayment.return_value = {"txnStatus": "PROCESSING", "status": "PROCESSING"}
    app.state.wedap = wedap
    return TestClient(app)


def _post(client: TestClient, path: str, body: dict) -> tuple[int, dict]:
    r = client.post(
        f"/api/v1/loans/{path}",
        json=body,
        headers={**HEADERS, "Idempotency-Key": body["bizSeqNo"]},
    )
    return r.status_code, r.json()


def _order_count(app) -> int:  # type: ignore[no-untyped-def]
    from sqlalchemy import func, select

    from app.models.txn import BankTxnOrder

    async def _run() -> int:
        async with app.state.session_factory() as session:
            return int((await session.execute(select(func.count(BankTxnOrder.id)))).scalar_one())

    return asyncio.run(_run())


# ── 放款 ───────────────────────────────────────────────────────────────────────


def test_real_shape_disbursement_accepted(client: TestClient) -> None:
    """回归护栏：实测形态必须仍被放行（防校验写过严）。"""
    status, body = _post(client, "p2p-disbursements", REAL_DISBURSE)
    assert status == 200, body


@pytest.mark.parametrize("field", DISBURSEMENT_REQUIRED)
def test_disbursement_missing_top_level_rejected(client: TestClient, field: str) -> None:
    body = {k: v for k, v in REAL_DISBURSE.items() if k != field}
    status, resp = _post(client, "p2p-disbursements", body)
    assert status == 400
    assert resp["error"]["code"] == "GW_400_VALIDATION"
    assert field in resp["error"]["message"]


@pytest.mark.parametrize("field", DISBURSEMENT_INFO_REQUIRED)
def test_disbursement_info_missing_required_rejected(client: TestClient, field: str) -> None:
    info = {k: v for k, v in REAL_DISBURSE["disbursementInfo"].items() if k != field}
    status, resp = _post(client, "p2p-disbursements", {**REAL_DISBURSE, "disbursementInfo": info})
    assert status == 400
    assert field in resp["error"]["message"]
    assert "disbursementInfo" in resp["error"]["message"]


@pytest.mark.parametrize("field", DISBURSEMENT_LENDER_REQUIRED)
def test_disbursement_lender_missing_required_rejected(client: TestClient, field: str) -> None:
    lender = {k: v for k, v in REAL_DISBURSE["lenders"][0].items() if k != field}
    status, resp = _post(client, "p2p-disbursements", {**REAL_DISBURSE, "lenders": [lender]})
    assert status == 400
    assert field in resp["error"]["message"]
    assert "lenders[0]" in resp["error"]["message"]


def test_disbursement_field_error_leaves_no_order_row(client: TestClient) -> None:
    """本次改动的真正目的：纯字段错误不得在资金台账留下 FAILED 垃圾单。"""
    body = {k: v for k, v in REAL_DISBURSE.items() if k != "channelId"}
    status, _ = _post(client, "p2p-disbursements", body)
    assert status == 400
    assert _order_count(client.app) == 0
    assert client.app.state.wedap.p2p_disbursement.await_count == 0  # type: ignore[union-attr]


# ── 还款 ───────────────────────────────────────────────────────────────────────


def test_real_shape_repayment_accepted(client: TestClient) -> None:
    status, body = _post(client, "p2p-repayments", REAL_REPAY)
    assert status == 200, body


@pytest.mark.parametrize("field", REPAYMENT_REQUIRED)
def test_repayment_missing_top_level_rejected(client: TestClient, field: str) -> None:
    body = {k: v for k, v in REAL_REPAY.items() if k != field}
    status, resp = _post(client, "p2p-repayments", body)
    assert status == 400
    assert field in resp["error"]["message"]


@pytest.mark.parametrize("field", REPAYMENT_INFO_REQUIRED)
def test_repayment_info_missing_required_rejected(client: TestClient, field: str) -> None:
    info = {k: v for k, v in REAL_REPAY["repaymentInfo"].items() if k != field}
    status, resp = _post(client, "p2p-repayments", {**REAL_REPAY, "repaymentInfo": info})
    assert status == 400
    assert field in resp["error"]["message"]
    assert "repaymentInfo" in resp["error"]["message"]


@pytest.mark.parametrize("field", REPAYMENT_LENDER_REQUIRED)
def test_repayment_lender_missing_required_rejected(client: TestClient, field: str) -> None:
    lender = {k: v for k, v in REAL_REPAY["lenders"][0].items() if k != field}
    status, resp = _post(client, "p2p-repayments", {**REAL_REPAY, "lenders": [lender]})
    assert status == 400
    assert field in resp["error"]["message"]
    assert "lenders[0]" in resp["error"]["message"]


def test_repayment_zero_interest_is_valid(client: TestClient) -> None:
    """interestAmount=0 是合法值（无息期还款），不得被「必填」判成缺失。

    wedap 的约束是 @NotNull 而非「非零」——把 0 判成缺会拦掉真实业务报文。
    """
    info = {**REAL_REPAY["repaymentInfo"], "interestAmount": "0.0000", "principalAmount": "1.0000"}
    lender = {**REAL_REPAY["lenders"][0], "interestAmount": "0.0000", "principalAmount": "1.0000"}
    status, body = _post(
        client, "p2p-repayments", {**REAL_REPAY, "repaymentInfo": info, "lenders": [lender]}
    )
    assert status == 200, body


def test_repayment_field_error_leaves_no_order_row(client: TestClient) -> None:
    body = {k: v for k, v in REAL_REPAY.items() if k != "channelId"}
    status, _ = _post(client, "p2p-repayments", body)
    assert status == 400
    assert _order_count(client.app) == 0
    assert client.app.state.wedap.p2p_repayment.await_count == 0  # type: ignore[union-attr]
