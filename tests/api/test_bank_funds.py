"""北向银行资金 API 测试：collect-from-users + distribute-to-users。"""

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
    "Idempotency-Key": "CLT-20260611-0001234567890",
}
COLLECT_BODY = {
    "bizSeqNo": "CLT-20260611-0001234567890",
    "totalAmount": "500.0000",
    "currencyCode": "USD",
    "userList": [{"userId": "U1", "amount": "500.0000"}],
}
DISTRIBUTE_BODY = {
    # 分发 wedap 真契约：顶层 currencyCode + recipients[].distributeAmount（无 totalAmount）。
    "bizSeqNo": "DST-20260611-0001234567890",
    "currencyCode": "USD",
    "recipients": [{"userId": "U2", "distributeAmount": "200.0000", "currencyCode": "USD"}],
}


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
    app.state.wedap = wedap
    return TestClient(app)


# ── collect-from-users ────────────────────────────────────────────────────────


def test_collect_accepted_envelope(client: TestClient) -> None:
    """受理 → 200 + success=True + txnStatus=PROCESSING + bizSeqNo 回显。"""
    r = client.post("/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["data"]["txnStatus"] == "PROCESSING"
    assert body["data"]["bizSeqNo"] == COLLECT_BODY["bizSeqNo"]
    assert body["trace_id"]


def test_collect_idempotent_replay_no_extra_call(client: TestClient) -> None:
    """同 key 同 payload 二次提交：外呼只发生 1 次。"""
    client.post("/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS)
    client.post("/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS)
    assert client.app.state.wedap.collect_from_users.await_count == 1  # type: ignore[union-attr]


def test_collect_missing_total_amount_422(client: TestClient) -> None:
    """totalAmount 字段缺失 → Pydantic 必填校验 → 422。"""
    body_no_amount = {k: v for k, v in COLLECT_BODY.items() if k != "totalAmount"}
    r = client.post("/api/v1/bank-funds/collect-from-users", json=body_no_amount, headers=HEADERS)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "GW_422_VALIDATION"


def test_collect_invalid_amount_str_400(client: TestClient) -> None:
    """totalAmount 无法解析为 Decimal → 400 GW_400_VALIDATION。"""
    body_bad = {**COLLECT_BODY, "totalAmount": "not-a-number"}
    r = client.post("/api/v1/bank-funds/collect-from-users", json=body_bad, headers=HEADERS)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "GW_400_VALIDATION"


def test_collect_bad_biz_seq_no_400(client: TestClient) -> None:
    """bizSeqNo 格式不合规 → submit_order 内 ValueError → 400 GW_400_VALIDATION。"""
    bad_key = "WB-1704067200000-COLLECT-10-0001-123456"
    body_bad = {**COLLECT_BODY, "bizSeqNo": bad_key}
    h = {**HEADERS, "Idempotency-Key": bad_key}
    r = client.post("/api/v1/bank-funds/collect-from-users", json=body_bad, headers=h)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "GW_400_VALIDATION"


# ── distribute-to-users ───────────────────────────────────────────────────────


def test_distribute_accepted_envelope(client: TestClient) -> None:
    """受理 → 200 + success=True + txnStatus=PROCESSING + bizSeqNo 回显。"""
    h = {**HEADERS, "Idempotency-Key": DISTRIBUTE_BODY["bizSeqNo"], "X-Request-Id": "req-2"}
    r = client.post("/api/v1/bank-funds/distribute-to-users", json=DISTRIBUTE_BODY, headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["data"]["txnStatus"] == "PROCESSING"
    assert body["data"]["bizSeqNo"] == DISTRIBUTE_BODY["bizSeqNo"]
    assert body["trace_id"]


def test_distribute_idempotent_replay_no_extra_call(client: TestClient) -> None:
    """同 key 同 payload 二次提交：外呼只发生 1 次。"""
    h = {**HEADERS, "Idempotency-Key": DISTRIBUTE_BODY["bizSeqNo"], "X-Request-Id": "req-dst"}
    client.post("/api/v1/bank-funds/distribute-to-users", json=DISTRIBUTE_BODY, headers=h)
    client.post("/api/v1/bank-funds/distribute-to-users", json=DISTRIBUTE_BODY, headers=h)
    assert (
        client.app.state.wedap.distribute_to_users.await_count == 1  # type: ignore[union-attr]
    )


def test_collect_same_key_different_payload_409(client: TestClient) -> None:
    """同 Idempotency-Key 不同 payload → 409 GW_409_IDEMPOTENCY。
    mutated 保持明细一致性（userList sum == totalAmount）以确保明细校验先通过，
    再由幂等层检测到 payload hash 变化触发 409。
    """
    client.post("/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS)
    mutated = {
        **COLLECT_BODY,
        "totalAmount": "999.0000",
        "userList": [{"userId": "U1", "amount": "999.0000"}],
    }
    r = client.post("/api/v1/bank-funds/collect-from-users", json=mutated, headers=HEADERS)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "GW_409_IDEMPOTENCY"


def test_collect_idempotency_key_mismatch_400(client: TestClient) -> None:
    """Idempotency-Key header 存在且与 bizSeqNo 不一致 → 400 GW_400_IDEMPOTENCY_KEY。"""
    h = {**HEADERS, "Idempotency-Key": "WRONG-KEY-9999"}
    r = client.post("/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=h)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "GW_400_IDEMPOTENCY_KEY"


def test_collect_no_idempotency_key_header_passes(client: TestClient) -> None:
    """无 Idempotency-Key header → 放行，以 bizSeqNo 为准。"""
    h = {k: v for k, v in HEADERS.items() if k != "Idempotency-Key"}
    r = client.post("/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=h)
    assert r.status_code == 200


def test_distribute_same_key_different_payload_409(client: TestClient) -> None:
    """distribute 同 Idempotency-Key 不同 payload → 409 GW_409_IDEMPOTENCY。
    覆盖 bank_funds._submit 内的 IdempotencyConflict 分支。
    """
    h = {**HEADERS, "Idempotency-Key": DISTRIBUTE_BODY["bizSeqNo"], "X-Request-Id": "req-dst2"}
    client.post("/api/v1/bank-funds/distribute-to-users", json=DISTRIBUTE_BODY, headers=h)
    mutated = {
        **DISTRIBUTE_BODY,
        "recipients": [{"userId": "U2", "distributeAmount": "999.0000", "currencyCode": "USD"}],
    }
    r = client.post("/api/v1/bank-funds/distribute-to-users", json=mutated, headers=h)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "GW_409_IDEMPOTENCY"


# ── 缺省 userList（wedap 真形态 body）修复验证 ───────────────────────────────────


def test_collect_without_userlist_passes_validation(client: TestClient) -> None:
    """wedap 真契约：user-collections 不含 userList 字段 → 校验跳过，200 受理。
    修复前：default_factory=list 物化 [] → validate_detail_consistency 误判 empty → 400。
    """
    body = {
        "bizSeqNo": "CLT-20260611-0002000000001",
        "totalAmount": "500.0000",
        "currencyCode": "USD",
        # 不传 userList
    }
    h = {**HEADERS, "Idempotency-Key": body["bizSeqNo"], "X-Request-Id": "req-no-ul"}
    r = client.post("/api/v1/bank-funds/collect-from-users", json=body, headers=h)
    assert r.status_code == 200, r.json()


def test_collect_without_userlist_payload_excludes_key(client: TestClient) -> None:
    """缺 userList 时，透传给 wedap 的 payload 不含 userList 键（契约 C：不注入伪字段）。"""
    body = {
        "bizSeqNo": "CLT-20260611-0002000000002",
        "totalAmount": "300.0000",
        "currencyCode": "USD",
    }
    h = {**HEADERS, "Idempotency-Key": body["bizSeqNo"], "X-Request-Id": "req-excl"}
    client.post("/api/v1/bank-funds/collect-from-users", json=body, headers=h)
    wedap_mock = client.app.state.wedap  # type: ignore[union-attr]
    # 取 submit_order 实际使用的 payload（通过 SubmitRequest.wedap_payload）
    call_args_str = str(wedap_mock.collect_from_users.call_args)
    assert "userList" not in call_args_str, (
        f"wedap payload 不应含 userList，实际参数：{call_args_str}"
    )


def test_collect_explicit_empty_userlist_400(client: TestClient) -> None:
    """显式传入 userList=[] → 仍触发 400（空列表不合法）。"""
    body = {
        "bizSeqNo": "CLT-20260611-0002000000003",
        "totalAmount": "500.0000",
        "currencyCode": "USD",
        "userList": [],
    }
    h = {**HEADERS, "Idempotency-Key": body["bizSeqNo"], "X-Request-Id": "req-empty-ul"}
    r = client.post("/api/v1/bank-funds/collect-from-users", json=body, headers=h)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "GW_400_VALIDATION"
    assert "empty userList" in r.json()["error"]["message"]


def test_distribute_without_recipients_passes_validation(client: TestClient) -> None:
    """distribute-to-users 缺 recipients → 跳过明细校验（金额 Σ=0），200 受理（契约 C 薄透传）。"""
    body = {
        "bizSeqNo": "DST-20260611-0002000000004",
        "currencyCode": "USD",
    }
    h = {**HEADERS, "Idempotency-Key": body["bizSeqNo"], "X-Request-Id": "req-dst-no-rcp"}
    r = client.post("/api/v1/bank-funds/distribute-to-users", json=body, headers=h)
    assert r.status_code == 200, r.json()


def test_distribute_amount_summed_and_recipients_passthrough(client: TestClient) -> None:
    """分发金额 = Σ recipients[].distributeAmount；recipients+bankAccountNo 经 extra=allow 透传。"""
    body = {
        "bizSeqNo": "DST-20260611-0002000000010",
        "currencyCode": "USD",
        "recipients": [
            {
                "userId": "U1",
                "distributeAmount": "60.0000",
                "currencyCode": "USD",
                "bankAccountNo": "ACC1",
            },
            {
                "userId": "U2",
                "distributeAmount": "40.0000",
                "currencyCode": "USD",
                "bankAccountNo": "ACC2",
            },
        ],
    }
    h = {**HEADERS, "Idempotency-Key": body["bizSeqNo"], "X-Request-Id": "req-sum"}
    r = client.post("/api/v1/bank-funds/distribute-to-users", json=body, headers=h)
    assert r.status_code == 200, r.json()
    call_str = str(client.app.state.wedap.distribute_to_users.call_args)  # type: ignore[union-attr]
    assert "recipients" in call_str
    assert "bankAccountNo" in call_str and "ACC1" in call_str


def test_distribute_recipient_currency_mismatch_400(client: TestClient) -> None:
    """recipient.currencyCode 与顶层 currencyCode 不一致 → 400 GW_400_VALIDATION。"""
    body = {
        "bizSeqNo": "DST-20260611-0002000000011",
        "currencyCode": "USD",
        "recipients": [{"userId": "U1", "distributeAmount": "100.0000", "currencyCode": "EUR"}],
    }
    h = {**HEADERS, "Idempotency-Key": body["bizSeqNo"], "X-Request-Id": "req-cur"}
    r = client.post("/api/v1/bank-funds/distribute-to-users", json=body, headers=h)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "GW_400_VALIDATION"


def test_distribute_empty_recipients_400(client: TestClient) -> None:
    """显式 recipients=[] → 400 empty recipients。"""
    body = {
        "bizSeqNo": "DST-20260611-0002000000012",
        "currencyCode": "USD",
        "recipients": [],
    }
    h = {**HEADERS, "Idempotency-Key": body["bizSeqNo"], "X-Request-Id": "req-empty-rcp"}
    r = client.post("/api/v1/bank-funds/distribute-to-users", json=body, headers=h)
    assert r.status_code == 400
    assert "empty recipients" in r.json()["error"]["message"]


def test_distribute_recipient_without_amount_skips_sum(client: TestClient) -> None:
    """recipient 缺 distributeAmount（wedap 自动分配场景）→ sum 校验跳过，金额 Σ=0，200 受理。"""
    body = {
        "bizSeqNo": "DST-20260611-0002000000013",
        "currencyCode": "USD",
        "recipients": [{"userId": "U1", "currencyCode": "USD"}],
    }
    h = {**HEADERS, "Idempotency-Key": body["bizSeqNo"], "X-Request-Id": "req-no-amt"}
    r = client.post("/api/v1/bank-funds/distribute-to-users", json=body, headers=h)
    assert r.status_code == 200, r.json()
