"""北向贷款 API 测试：p2p-disbursements + p2p-repayments。"""

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
    "Idempotency-Key": "DSB-20260611-0001234567890",
}
BODY = {
    "bizSeqNo": "DSB-20260611-0001234567890",
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


@pytest.fixture()
def client() -> TestClient:
    app = create_app()
    # 建表（sqlite :memory: engine 已在 create_app 内创建，需在使用前 create_all）
    asyncio.run(_create_tables(app.state.engine))
    wedap = AsyncMock()
    wedap.submit_disbursement.return_value = {"txnStatus": "PROCESSING"}
    wedap.submit_repayment.return_value = {"txnStatus": "PROCESSING"}
    app.state.wedap = wedap
    return TestClient(app)


async def _create_tables(engine) -> None:  # type: ignore[no-untyped-def]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def test_disbursement_accepted_envelope(client: TestClient) -> None:
    r = client.post("/api/v1/loans/p2p-disbursements", json=BODY, headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True and body["data"]["txnStatus"] == "PROCESSING"
    assert body["data"]["bizSeqNo"] == BODY["bizSeqNo"] and body["trace_id"]


def test_missing_tenant_header_400(client: TestClient) -> None:
    h = {k: v for k, v in HEADERS.items() if k != "X-Tenant-Id"}
    r = client.post("/api/v1/loans/p2p-disbursements", json=BODY, headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == "GW_400_HEADER"


def test_missing_request_id_header_400(client: TestClient) -> None:
    h = {k: v for k, v in HEADERS.items() if k != "X-Request-Id"}
    r = client.post("/api/v1/loans/p2p-disbursements", json=BODY, headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == "GW_400_HEADER"


def test_bad_biz_seq_no_400(client: TestClient) -> None:
    bad = {**BODY, "bizSeqNo": "WB-1704067200000-DISB-10-0001-123456"}
    r = client.post(
        "/api/v1/loans/p2p-disbursements",
        json=bad,
        headers={**HEADERS, "Idempotency-Key": bad["bizSeqNo"]},
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "GW_400_VALIDATION"


def test_missing_amount_400(client: TestClient) -> None:
    """disbursementInfo 缺 txnAmount → 422 validation（Pydantic 必填）。"""
    body_no_amount = {
        **BODY,
        "disbursementInfo": {"currencyCode": "USD", "userId": "U1", "userName": "u"},
    }
    r = client.post("/api/v1/loans/p2p-disbursements", json=body_no_amount, headers=HEADERS)
    assert r.status_code == 422
    assert "VALIDATION" in r.json()["error"]["code"]


def test_invalid_amount_zero_400(client: TestClient) -> None:
    """txnAmount=0 → 400 GW_400_VALIDATION（非正数）。"""
    body_zero = {
        **BODY,
        "disbursementInfo": {**BODY["disbursementInfo"], "txnAmount": "0"},
    }
    r = client.post("/api/v1/loans/p2p-disbursements", json=body_zero, headers=HEADERS)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "GW_400_VALIDATION"


def test_same_key_different_payload_409(client: TestClient) -> None:
    client.post("/api/v1/loans/p2p-disbursements", json=BODY, headers=HEADERS)
    # The variant must itself be valid (else it hits the sum guard 400 before reaching the
    # idempotency 409): bump both total and lenders to 999.
    mutated = {
        **BODY,
        "disbursementInfo": {**BODY["disbursementInfo"], "txnAmount": "999.0000"},
        "lenders": [{"userId": "L1", "lendAmount": "999.0000", "currencyCode": "USD"}],
    }
    r = client.post("/api/v1/loans/p2p-disbursements", json=mutated, headers=HEADERS)
    assert r.status_code == 409 and r.json()["error"]["code"] == "GW_409_IDEMPOTENCY"


def test_idempotent_replay_no_extra_call(client: TestClient) -> None:
    """同 key 同 payload 二次调用：wedap 外呼只发生 1 次（InFlight → PROCESSING）。"""
    client.post("/api/v1/loans/p2p-disbursements", json=BODY, headers=HEADERS)
    client.post("/api/v1/loans/p2p-disbursements", json=BODY, headers=HEADERS)
    # submit_disbursement 实际只在 submit_order 内通过 wedap_call 调用；
    # 第一次：InFlight → PROCESSING（mock 返回值），第二次 InFlight 直接走 PROCESSING 路径
    # 因为 session_factory 是 in-memory SQLite，第一次注册但 record_response 未调用 → InFlight
    assert client.app.state.wedap.submit_disbursement.await_count == 1  # type: ignore[union-attr]


def test_repayment_endpoint_works(client: TestClient) -> None:
    body = {
        "bizSeqNo": "RPY-20260611-0001234567890",
        "transType": "REPAYMENT",
        "repaymentInfo": {"txnAmount": "50.0000", "currencyCode": "USD"},
        "lenders": [{"userId": "L1", "txnAmount": "50.0000", "currencyCode": "USD"}],
    }
    r = client.post(
        "/api/v1/loans/p2p-repayments",
        json=body,
        headers={**HEADERS, "Idempotency-Key": body["bizSeqNo"]},
    )
    assert r.json()["data"]["bizSeqNo"] == body["bizSeqNo"]


def test_disbursement_idempotency_key_mismatch_400(client: TestClient) -> None:
    """Idempotency-Key header 存在且与 bizSeqNo 不一致 → 400 GW_400_IDEMPOTENCY_KEY。"""
    h = {**HEADERS, "Idempotency-Key": "WRONG-KEY-9999"}
    r = client.post("/api/v1/loans/p2p-disbursements", json=BODY, headers=h)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "GW_400_IDEMPOTENCY_KEY"


def test_disbursement_no_idempotency_key_header_passes(client: TestClient) -> None:
    """无 Idempotency-Key header → 放行，以 bizSeqNo 为准。"""
    h = {k: v for k, v in HEADERS.items() if k != "Idempotency-Key"}
    r = client.post("/api/v1/loans/p2p-disbursements", json=BODY, headers=h)
    assert r.status_code == 200


# ── 缺省 lenders（wedap 真形态 body）修复验证 ─────────────────────────────────────


def test_disbursement_without_lenders_rejected_400(client: TestClient) -> None:
    """wedap DisbursementAdapterRequest.lenders is @NotEmpty: p2p-disbursements missing lenders
    must be rejected 400 by the gateway itself (require_detail=True), no longer relying on the
    wedap backstop (codex R1 finding fix).
    """
    body = {
        "bizSeqNo": "DSB-20260611-0002000000001",
        "channelId": "LEN",
        "transType": "DISBURSEMENT",
        "disbursementInfo": {
            "txnAmount": "100.0000",
            "currencyCode": "USD",
            "userId": "U1",
            "userName": "u",
        },
        # 不传 lenders
    }
    h = {**HEADERS, "Idempotency-Key": body["bizSeqNo"], "X-Request-Id": "req-no-lend"}
    r = client.post("/api/v1/loans/p2p-disbursements", json=body, headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == "GW_400_VALIDATION"
    assert "missing lenders" in r.json()["error"]["message"]


def test_repayment_without_lenders_rejected_400(client: TestClient) -> None:
    """wedap RepaymentAdapterRequest.lenders is @NotEmpty: p2p-repayments missing lenders
    must be rejected 400 by the gateway itself (require_detail=True) -- guards the repayment
    call-site wiring against accidental deletion (codex R2 finding).
    """
    body = {
        "bizSeqNo": "RPY-20260611-0002000000002",
        "transType": "REPAYMENT",
        "repaymentInfo": {"txnAmount": "50.0000", "currencyCode": "USD"},
        # no lenders
    }
    h = {**HEADERS, "Idempotency-Key": body["bizSeqNo"], "X-Request-Id": "req-no-lend-rpy"}
    r = client.post("/api/v1/loans/p2p-repayments", json=body, headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == "GW_400_VALIDATION"
    assert "missing lenders" in r.json()["error"]["message"]


def test_disbursement_without_lenders_shortcircuits_before_wedap(client: TestClient) -> None:
    """Missing lenders -> guard 400 and the downstream wedap call is not made (short-circuit,
    no leaked mid-flight outbound call).

    With require_detail=True, missing lenders returns 400 at the guard and submit_disbursement
    must not be reached; asserting "not called" adds the downstream-isolation guarantee beyond
    rejected_400 (which only checks the error envelope).
    """
    body = {
        "bizSeqNo": "DSB-20260611-0002000000002",
        "channelId": "LEN",
        "transType": "DISBURSEMENT",
        "disbursementInfo": {
            "txnAmount": "100.0000",
            "currencyCode": "USD",
            "userId": "U1",
            "userName": "u",
        },
    }
    h = {**HEADERS, "Idempotency-Key": body["bizSeqNo"], "X-Request-Id": "req-lend-excl"}
    r = client.post("/api/v1/loans/p2p-disbursements", json=body, headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == "GW_400_VALIDATION"
    wedap_mock = client.app.state.wedap  # type: ignore[union-attr]
    wedap_mock.submit_disbursement.assert_not_called()


def test_disbursement_explicit_empty_lenders_400(client: TestClient) -> None:
    """显式传入 lenders=[] → 仍触发 400（空列表不合法）。"""
    body = {
        "bizSeqNo": "DSB-20260611-0002000000003",
        "channelId": "LEN",
        "transType": "DISBURSEMENT",
        "disbursementInfo": {
            "txnAmount": "100.0000",
            "currencyCode": "USD",
            "userId": "U1",
            "userName": "u",
        },
        "lenders": [],
    }
    h = {**HEADERS, "Idempotency-Key": body["bizSeqNo"], "X-Request-Id": "req-empty-lend"}
    r = client.post("/api/v1/loans/p2p-disbursements", json=body, headers=h)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "GW_400_VALIDATION"
    assert "empty lenders" in r.json()["error"]["message"]


# ── 还款 lenders[].txnAmount 校验 + extra=allow 透传修复验证 ──────────────────────


def test_repayment_lenders_txnamount_sum_ok(client: TestClient) -> None:
    """还款 lenders[].txnAmount 之和 == repaymentInfo.txnAmount → 200（lending 按占比拆分）。"""
    body = {
        "bizSeqNo": "RPY-20260611-0002000000001",
        "transType": "REPAYMENT",
        "repaymentInfo": {"txnAmount": "100.0000", "currencyCode": "USD"},
        "lenders": [
            {"userId": "L1", "txnAmount": "60.0000"},
            {"userId": "L2", "txnAmount": "40.0000"},
        ],
    }
    h = {**HEADERS, "Idempotency-Key": body["bizSeqNo"], "X-Request-Id": "req-rpy-sum"}
    r = client.post("/api/v1/loans/p2p-repayments", json=body, headers=h)
    assert r.status_code == 200, r.json()


def test_repayment_lenders_txnamount_mismatch_400(client: TestClient) -> None:
    """lenders[].txnAmount 之和 != repaymentInfo.txnAmount → 400（原 shareAmount 会漏判此错）。"""
    body = {
        "bizSeqNo": "RPY-20260611-0002000000002",
        "transType": "REPAYMENT",
        "repaymentInfo": {"txnAmount": "100.0000", "currencyCode": "USD"},
        "lenders": [
            {"userId": "L1", "txnAmount": "60.0000"},
            {"userId": "L2", "txnAmount": "30.0000"},
        ],
    }
    h = {**HEADERS, "Idempotency-Key": body["bizSeqNo"], "X-Request-Id": "req-rpy-mis"}
    r = client.post("/api/v1/loans/p2p-repayments", json=body, headers=h)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "GW_400_VALIDATION"


def test_repayment_with_fee_three_way_sum_ok(client: TestClient) -> None:
    """含费还款：txnAmount = Σlender.txnAmount + ΣfeeDeductions.feeAmount → 200。

    口径=含费总额（权威 wedap 契约 :169 + 上游 _align_amounts_for_baffle）。此前 gateway
    护栏只核 Σlender==total、feeDeductions 不入等式，含费还款被误判 400（20260720 联调 R4.6
    FINDING，FU-GW-REPAY-FEE-SUMGUARD-20260720-001）；本例锁定修复后放行。
    """
    body = {
        "bizSeqNo": "RPY-20260720-0002000000010",
        "transType": "REPAYMENT",
        "repaymentInfo": {"txnAmount": "3.0000", "currencyCode": "USD"},
        "lenders": [{"userId": "L1", "txnAmount": "2.8000"}],
        "feeDeductions": [{"feeType": "PENALTY", "feeAmount": "0.2000"}],
    }
    h = {**HEADERS, "Idempotency-Key": body["bizSeqNo"], "X-Request-Id": "req-rpy-fee-ok"}
    r = client.post("/api/v1/loans/p2p-repayments", json=body, headers=h)
    assert r.status_code == 200, r.json()
    # feeDeductions 仍透传给 wedap（extra=allow），未被护栏剪裁
    call_str = str(client.app.state.wedap.submit_repayment.call_args)  # type: ignore[union-attr]
    assert "feeDeductions" in call_str


def test_repayment_with_fee_sum_mismatch_400(client: TestClient) -> None:
    """含费还款 Σlender + Σfee != txnAmount → 400（三等式不平仍拦）。"""
    body = {
        "bizSeqNo": "RPY-20260720-0002000000011",
        "transType": "REPAYMENT",
        "repaymentInfo": {"txnAmount": "3.5000", "currencyCode": "USD"},
        "lenders": [{"userId": "L1", "txnAmount": "2.8000"}],
        "feeDeductions": [{"feeType": "PENALTY", "feeAmount": "0.2000"}],
    }
    h = {**HEADERS, "Idempotency-Key": body["bizSeqNo"], "X-Request-Id": "req-rpy-fee-mis"}
    r = client.post("/api/v1/loans/p2p-repayments", json=body, headers=h)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "GW_400_VALIDATION"


def test_repayment_extra_fields_passthrough(client: TestClient) -> None:
    """extra=allow：repaymentInfo 嵌套必填字段 + 顶层 lenders[] 全透传（修复前被静默丢致拒单）。"""
    body = {
        "bizSeqNo": "RPY-20260611-0002000000003",
        "transType": "REPAYMENT",
        "repaymentInfo": {
            "txnAmount": "100.0000",
            "currencyCode": "USD",
            "principalAmount": "90.0000",
            "interestAmount": "10.0000",
            "userId": "B1",
            "userName": "borrower",
            "repaymentType": "NORMAL",
        },
        "lenders": [{"userId": "L1", "txnAmount": "100.0000"}],
    }
    h = {**HEADERS, "Idempotency-Key": body["bizSeqNo"], "X-Request-Id": "req-rpy-pt"}
    r = client.post("/api/v1/loans/p2p-repayments", json=body, headers=h)
    assert r.status_code == 200, r.json()
    call_str = str(client.app.state.wedap.submit_repayment.call_args)  # type: ignore[union-attr]
    for f in ("principalAmount", "interestAmount", "repaymentType", "lenders"):
        assert f in call_str, f"wedap payload 应含 {f}，实际：{call_str}"


def test_disbursement_extra_fields_passthrough(client: TestClient) -> None:
    """extra=allow：disbursementInfo.postscript + 顶层 feeDeductions 透传 wedap（修复前被丢）。"""
    body = {
        "bizSeqNo": "DSB-20260611-0002000000009",
        "channelId": "LEN",
        "transType": "DISBURSEMENT",
        "disbursementInfo": {
            "txnAmount": "100.0000",
            "currencyCode": "USD",
            "userId": "U1",
            "userName": "u",
            "postscript": "loan-disb-memo",
        },
        "lenders": [{"userId": "L1", "lendAmount": "100.0000", "currencyCode": "USD"}],
        "feeDeductions": [{"feeType": "PLATFORM", "amount": "1.0000"}],
    }
    h = {**HEADERS, "Idempotency-Key": body["bizSeqNo"], "X-Request-Id": "req-disb-pt"}
    r = client.post("/api/v1/loans/p2p-disbursements", json=body, headers=h)
    assert r.status_code == 200, r.json()
    call_str = str(client.app.state.wedap.submit_disbursement.call_args)  # type: ignore[union-attr]
    assert "postscript" in call_str and "feeDeductions" in call_str
