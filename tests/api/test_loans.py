"""北向贷款 API 测试：p2p-disbursements + p2p-repayments。"""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.clients.wedap import WedapError
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
    # 还款受理响应按 DTC 新契约（v0.6.1 §4.2）：status 非 txnStatus，另有 detailStatus/
    # debtSettled/globalTxId。mock 须与真契约同形，否则测试会绿在一个 wedap 不会返回的形状上。
    wedap.submit_repayment.return_value = {
        "bizSeqNo": "RPMT-20260810-0001234567890",
        "globalTxId": "GT20260727000001",
        "status": "PROCESSING",
        "detailStatus": "PROCESSING",
        "debtSettled": False,
    }
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
        **REPAY_BODY,
        "bizSeqNo": "RPY-20260611-0001234567890",
        "repaymentInfo": {**REPAY_BODY["repaymentInfo"], "txnAmount": "50.0000"},
        "lenders": [{**REPAY_BODY["lenders"][0], "txnAmount": "50.0000"}],
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
        **{k: v for k, v in REPAY_BODY.items() if k != "lenders"},
        "bizSeqNo": "RPY-20260611-0002000000002",
        "repaymentInfo": {**REPAY_BODY["repaymentInfo"], "txnAmount": "50.0000"},
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
        **REPAY_BODY,
        "bizSeqNo": "RPY-20260611-0002000000001",
        "repaymentInfo": {**REPAY_BODY["repaymentInfo"], "txnAmount": "100.0000"},
        "lenders": [
            {
                **REPAY_BODY["lenders"][0],
                "userId": "L1",
                "txnAmount": "60.0000",
                "principalAmount": "48.0000",
                "interestAmount": "12.0000",
            },
            {
                **REPAY_BODY["lenders"][0],
                "userId": "L2",
                "txnAmount": "40.0000",
                "principalAmount": "32.0000",
                "interestAmount": "8.0000",
            },
        ],
    }
    h = {**HEADERS, "Idempotency-Key": body["bizSeqNo"], "X-Request-Id": "req-rpy-sum"}
    r = client.post("/api/v1/loans/p2p-repayments", json=body, headers=h)
    assert r.status_code == 200, r.json()


def test_repayment_lenders_txnamount_mismatch_400(client: TestClient) -> None:
    """lenders[].txnAmount 之和 != repaymentInfo.txnAmount → 400（原 shareAmount 会漏判此错）。"""
    body = {
        **REPAY_BODY,
        "bizSeqNo": "RPY-20260611-0002000000002",
        "repaymentInfo": {**REPAY_BODY["repaymentInfo"], "txnAmount": "100.0000"},
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
        **REPAY_BODY,
        "bizSeqNo": "RPY-20260720-0002000000010",
        "repaymentInfo": {**REPAY_BODY["repaymentInfo"], "txnAmount": "3.0000"},
        "lenders": [{**REPAY_BODY["lenders"][0], "txnAmount": "2.8000"}],
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
        **REPAY_BODY,
        "bizSeqNo": "RPY-20260720-0002000000011",
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
        **REPAY_BODY,
        "bizSeqNo": "RPY-20260611-0002000000003",
        "repaymentInfo": {
            "txnAmount": "100.0000",
            "currencyCode": "USD",
            "principalAmount": "90.0000",
            "interestAmount": "10.0000",
            "userId": "B1",
            "userName": "borrower",
            "repaymentType": "NORMAL",
        },
        "lenders": [
            {
                **REPAY_BODY["lenders"][0],
                "userId": "L1",
                "txnAmount": "100.0000",
                "principalAmount": "90.0000",
                "interestAmount": "10.0000",
            }
        ],
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


# ── 还款专用状态查询 · 对接文档 v0.6.1 §4.2「还款状态查询（专用接口）」 ──────────────

REPAY_BODY = {
    "bizSeqNo": "RPMT-20260810-0001234567890",
    "channelId": "LEN",
    "transType": "REPAYMENT",
    "loanNo": "LN20250205000001",
    # wedap 还款必填集（2026-08-11 实测，app/domain/wedap_contract.REPAYMENT_INFO_REQUIRED /
    # REPAYMENT_LENDER_REQUIRED）：原 fixture 只有 txnAmount+currencyCode，正是「缺字段报文
    # 配 mock 成功」的测试盲点——真报文会被 wedap 400 拒（traceId f1117af9…）。
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
REPAY_HEADERS = {**HEADERS, "Idempotency-Key": REPAY_BODY["bizSeqNo"]}
WEDAP_STATUS_DATA = {
    "bizSeqNo": REPAY_BODY["bizSeqNo"],
    "globalTxId": "GT20260727000001",
    "status": "PROCESSING",
    "detailStatus": "PENDING_MANUAL",
    "debtSettled": False,
    "strandedAmount": "55.0000",
    "steps": [
        {
            "stepType": "COLLECT",
            "stepSeq": 1,
            "busiOrderNo": "BO-1",
            "bankRefNo": "BR-1",
            "amount": "110.0000",
            "currencyCode": "USD",
            "payerAccountNo": "ACC001",
            "payeeAccountNo": "INT00101001USD",
            "status": "SUCCESS",
        },
        {
            "stepType": "DISTRIBUTE",
            "stepSeq": 2,
            "busiOrderNo": "BO-2",
            "bankRefNo": None,
            "amount": "55.0000",
            "currencyCode": "USD",
            "payerAccountNo": "INT00101001USD",
            "payeeAccountNo": "ACC002",
            "status": "PENDING_MANUAL",
            "failReason": "counter hold",
            "failCategory": "TECHNICAL",
        },
    ],
}


def _accept_repayment(client: TestClient) -> None:
    r = client.post("/api/v1/loans/p2p-repayments", json=REPAY_BODY, headers=REPAY_HEADERS)
    assert r.status_code == 200


def test_repayment_status_query_exposes_steps_and_debt_settled(client: TestClient) -> None:
    """v0.5.0 专用状态查询：debtSettled(核销依据) 与逐笔 steps[](对账) 只此接口提供，
    5.5 通用查询不含——故 gateway 必须单独透传，不能只靠 /bank-funds/status。"""
    _accept_repayment(client)
    client.app.state.wedap.query_repayment_status.return_value = WEDAP_STATUS_DATA
    r = client.get(f"/api/v1/loans/p2p-repayments/{REPAY_BODY['bizSeqNo']}/status", headers=HEADERS)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["bizSeqNo"] == REPAY_BODY["bizSeqNo"]
    assert data["orderStatus"] == "SUBMITTED"
    assert data["wedap"]["debtSettled"] is False
    assert data["wedap"]["strandedAmount"] == "55.0000"
    assert [s["stepType"] for s in data["wedap"]["steps"]] == ["COLLECT", "DISTRIBUTE"]
    # 专用接口按 bizSeqNo 路径参数查询，无需 transType/oriReqDate（与 5.5 通用查询不同）
    client.app.state.wedap.query_repayment_status.assert_awaited_once()
    kwargs = client.app.state.wedap.query_repayment_status.await_args.kwargs
    assert kwargs["biz_seq_no"] == REPAY_BODY["bizSeqNo"]
    assert kwargs["tenant_id"] == "OCBC"


def test_repayment_status_query_unknown_order_404(client: TestClient) -> None:
    """本地无此单 → 404，不打 wedap（防拿外部系统当存在性判据）。"""
    r = client.get(
        "/api/v1/loans/p2p-repayments/RPMT-20260810-9999999999999/status", headers=HEADERS
    )
    assert r.status_code == 404 and r.json()["error"]["code"] == "GW_404_ORDER"
    client.app.state.wedap.query_repayment_status.assert_not_awaited()


def test_repayment_status_query_tenant_isolated_404(client: TestClient) -> None:
    """跨租户查不到（租户隔离）：单属 OCBC，另一租户查 → 404 且不打 wedap。"""
    _accept_repayment(client)
    r = client.get(
        f"/api/v1/loans/p2p-repayments/{REPAY_BODY['bizSeqNo']}/status",
        headers={**HEADERS, "X-Tenant-Id": "WBTHK01"},
    )
    assert r.status_code == 404
    client.app.state.wedap.query_repayment_status.assert_not_awaited()


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (httpx.ConnectTimeout("t"), "timeout"),
        (
            httpx.HTTPStatusError(
                "e", request=httpx.Request("GET", "http://x"), response=httpx.Response(500)
            ),
            "http_error",
        ),
        (WedapError("500", "boom"), "wedap_error"),
    ],
)
def test_repayment_status_query_degrades_when_wedap_down(
    client: TestClient, exc: Exception, reason: str
) -> None:
    """wedap 不可用 → 降级 unavailable + 本地 orderStatus 仍可读，不向外抛 5xx
    （同 /bank-funds/status 既有降级口径）。"""
    _accept_repayment(client)
    client.app.state.wedap.query_repayment_status.side_effect = exc
    r = client.get(f"/api/v1/loans/p2p-repayments/{REPAY_BODY['bizSeqNo']}/status", headers=HEADERS)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["wedap"] == {"unavailable": True, "reason": reason}
    assert data["orderStatus"] == "SUBMITTED"


# ── response_model 不得静默丢字段（挂 response_model 的直接风险） ─────────────────


def test_repayment_ack_response_model_keeps_dtc_fields(client: TestClient) -> None:
    """还款受理响应经 RepaymentAck 序列化后，DTC 三字段必须仍在线格式里。

    挂 response_model 的最大风险是**静默过滤**：模型没声明的键会被丢掉且不报错。
    debtSettled 正是本次要暴露给上游的核销依据，一旦被过滤，上游永远拿不到——
    而单测只断言 submit 层返回值的话完全发现不了（那层没经过 response_model）。
    """
    body = {
        **REPAY_BODY,
        "bizSeqNo": "RPY-20260611-0009999999901",
        "repaymentInfo": {**REPAY_BODY["repaymentInfo"], "txnAmount": "50.0000"},
        "lenders": [{**REPAY_BODY["lenders"][0], "txnAmount": "50.0000"}],
    }
    r = client.post(
        "/api/v1/loans/p2p-repayments",
        json=body,
        headers={**HEADERS, "Idempotency-Key": body["bizSeqNo"]},
    )
    assert r.status_code == 200
    data = r.json()["data"]
    # fixture 的 submit_repayment mock 按 DTC 契约返回这三个字段
    assert data["debtSettled"] is False
    assert data["globalTxId"] == "GT20260727000001"
    assert data["detailStatus"] == "PROCESSING"
    # 既有最小契约字段一并在场
    assert data["txnStatus"] == "PROCESSING"
    assert data["bizSeqNo"] == body["bizSeqNo"]
    assert data["orderStatus"] == "SUBMITTED"


def test_disbursement_ack_response_model_keeps_min_contract(client: TestClient) -> None:
    """放款受理响应经 SubmitAck 序列化后最小字段集不缺；且不冒出还款专属字段。"""
    r = client.post("/api/v1/loans/p2p-disbursements", json=BODY, headers=HEADERS)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["txnStatus"] == "PROCESSING"
    assert data["bizSeqNo"] == BODY["bizSeqNo"]
    assert data["orderStatus"] == "SUBMITTED"
    # exclude_unset：放款不该出现还款专属字段，也不该有 errorCode: null 噪声
    assert "debtSettled" not in data
    assert "errorCode" not in data


def test_repayment_replay_shape_identical_after_response_model(client: TestClient) -> None:
    """幂等重放经 response_model 后与首次**逐字段一致**（含 DTC 三字段）。

    重放返回的是冻结的 first_response，同样要过一遍 response_model；若模型漏声明某个
    键，首次和重放会一起丢——但更隐蔽的是二者不一致（如重放补出 null）。
    """
    body = {
        **REPAY_BODY,
        "bizSeqNo": "RPY-20260611-0009999999902",
        "repaymentInfo": {**REPAY_BODY["repaymentInfo"], "txnAmount": "50.0000"},
        "lenders": [{**REPAY_BODY["lenders"][0], "txnAmount": "50.0000"}],
    }
    h = {**HEADERS, "Idempotency-Key": body["bizSeqNo"]}
    first = client.post("/api/v1/loans/p2p-repayments", json=body, headers=h).json()["data"]
    replay = client.post("/api/v1/loans/p2p-repayments", json=body, headers=h).json()["data"]
    assert replay == first
    assert replay["debtSettled"] is False and replay["globalTxId"] == "GT20260727000001"


def test_inflight_shape_representable_by_response_model() -> None:
    """in-flight 形态 {txnStatus,bizSeqNo,inFlight}（无 orderStatus）能被 RepaymentAck 承载。

    该路径由 submit_order 的 IdempotencyInFlight 分支产生（见 test_submit.py 同名覆盖），
    在 API 层难以稳定构造，故在模型层直接验：inFlight 保留、未设置的 orderStatus 经
    exclude_unset 不出现（不能补成 null——那会让上游把「零查询路径」误读为「无台账态」）。
    """
    from app.api.v1.loans import RepaymentAck

    inflight = {
        "txnStatus": "PROCESSING",
        "bizSeqNo": "RPY-1",
        "inFlight": True,
        # v2.2 §8.2 typed 字段：durable operation 已在 dispatch 前落库，故 in-flight
        # 一样给得出；outcome=PENDING（本路径不读 order 行，不知上游是否已受理）
        "outcome": "PENDING",
        "operationStatus": "PENDING",
        "retryPolicy": "POLL_STATUS",
        "resubmitAllowed": False,
        "operationId": "RPY-1",
        "statusUrl": "/api/v1/loans/p2p-repayments/RPY-1/status",
    }
    ack = RepaymentAck.model_validate(inflight)
    dumped = ack.model_dump(exclude_unset=True, mode="json")
    assert dumped == inflight


# ── MONEY_WRITE typed 字段经 response_model 的线格式（v2.2 §8.2）──────────────────


def test_repayment_typed_fields_survive_response_model(client: TestClient) -> None:
    """typed 字段必须真的出现在**线格式**里。

    模型漏声明任一字段时 FastAPI 会静默丢弃（response_model 过滤），服务层测试全绿而
    调用方什么也拿不到——这正是 debtSettled 当年「只靠口头传达」的同一个坑。
    还款的 statusUrl 必须指向专用查单端点。
    """
    r = client.post("/api/v1/loans/p2p-repayments", json=REPAY_BODY, headers=REPAY_HEADERS)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["outcome"] == "ACCEPTED"  # 上游已受理，未入账
    assert data["operationStatus"] == "PENDING"
    assert data["retryPolicy"] == "POLL_STATUS"
    assert data["resubmitAllowed"] is False
    assert data["operationId"] == REPAY_BODY["bizSeqNo"]
    assert data["statusUrl"] == (f"/api/v1/loans/p2p-repayments/{REPAY_BODY['bizSeqNo']}/status")


def test_disbursement_wedap_5xx_typed_fields_say_unknown(client: TestClient) -> None:
    """上游 5xx：线格式给 UNKNOWN/RECONCILING/POLL_STATUS —— 本仓「不判 FAILED」的资金
    安全设计，第一次用 v2.2 词汇对调用方讲清楚。HTTP 状态码仍是 200（翻转是后续批次）。"""
    client.app.state.wedap.submit_disbursement.side_effect = httpx.HTTPStatusError(  # type: ignore[union-attr]
        "boom", request=httpx.Request("POST", "http://x"), response=httpx.Response(503)
    )
    r = client.post("/api/v1/loans/p2p-disbursements", json=BODY, headers=HEADERS)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["txnStatus"] == "RESULT_UNKNOWN"
    assert data["outcome"] == "UNKNOWN"
    assert data["operationStatus"] == "RECONCILING"
    assert data["retryPolicy"] == "POLL_STATUS"
    assert data["resubmitAllowed"] is False
    assert data["statusUrl"] == f"/api/v1/bank-funds/status?bizSeqNo={BODY['bizSeqNo']}"


def test_disbursement_wedap_business_reject_typed_fields_say_not_applied(
    client: TestClient,
) -> None:
    """wedap 在 HTTP 4xx 上结构化拒绝：NOT_APPLIED + CORRECT_AND_NEW_INTENT，不许同键重提。"""
    client.app.state.wedap.submit_disbursement.side_effect = WedapError(  # type: ignore[union-attr]
        "422", "可用余额不足", http_status=422
    )
    r = client.post("/api/v1/loans/p2p-disbursements", json=BODY, headers=HEADERS)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["outcome"] == "NOT_APPLIED"
    assert data["operationStatus"] == "REJECTED"
    assert data["retryPolicy"] == "CORRECT_AND_NEW_INTENT"
    assert data["resubmitAllowed"] is False


def test_disbursement_envelope_drift_never_says_not_applied(client: TestClient) -> None:
    """线格式回归（BLOCKER-1）：wedap 200 + 顶层缺 code → UNKNOWN，绝不 NOT_APPLIED。"""
    client.app.state.wedap.submit_disbursement.side_effect = WedapError(  # type: ignore[union-attr]
        "None", "<no error message>", http_status=200
    )
    r = client.post("/api/v1/loans/p2p-disbursements", json=BODY, headers=HEADERS)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["outcome"] == "UNKNOWN"
    assert data["operationStatus"] == "RECONCILING"
    assert data["retryPolicy"] == "POLL_STATUS"


def test_disbursement_sync_success_omits_outcome_on_the_wire(client: TestClient) -> None:
    """同步完成：线格式**不出现** outcome（v2.2 禁止为字段齐全填无意义默认值）。"""
    client.app.state.wedap.submit_disbursement.return_value = {"txnStatus": "SUCCESS"}  # type: ignore[union-attr]
    r = client.post("/api/v1/loans/p2p-disbursements", json=BODY, headers=HEADERS)
    data = r.json()["data"]
    assert "outcome" not in data
    assert data["operationStatus"] == "SUCCEEDED"
    assert data["retryPolicy"] == "NEVER"
