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
    "transType": "LOAN_COLLECT",
    "txnAmount": "500.0000",
    "currencyCode": "USD",
    # wedap 归集必填集（2026-08-11 实测，app/domain/wedap_contract.COLLECT_REQUIRED）：
    # 原 fixture 只有前四键，缺了这五个——「测试全绿」因此从来测不出真报文会被 wedap 拒。
    "channelId": "LEN",
    "userId": "U1",
    "custAccountNo": "1001234567890",
    "bankAccountNo": "9999617459809900000215",
    "bankAccountName": "TEST COLLECTION ACCOUNT",
    "userList": [{"userId": "U1", "amount": "500.0000"}],
}
DISTRIBUTE_BODY = {
    # 分发 wedap 真契约：顶层 currencyCode + 顶层付款方账户 + recipients[].distributeAmount
    # （无 totalAmount；账户身份字段只能在顶层，2026-08-11 实测）。
    "bizSeqNo": "DST-20260611-0001234567890",
    "transType": "BANK_FUND_DISTRIBUTE",
    "currencyCode": "USD",
    "channelId": "LEN",
    "bankAccountNo": "9999617459809900000215",
    "bankAccountName": "TEST PLATFORM ACCOUNT",
    "recipients": [
        {
            "userId": "U2",
            "userName": "TEST RECIPIENT",
            "custAccountNo": "1001234567891",
            "distributeAmount": "200.0000",
            "currencyCode": "USD",
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


def test_collect_missing_amount_400(client: TestClient) -> None:
    """txnAmount 缺失 → 400（旧 totalAmount 回退已删，金额只认扁平 txnAmount）。"""
    body_no_amount = {k: v for k, v in COLLECT_BODY.items() if k != "txnAmount"}
    r = client.post("/api/v1/bank-funds/collect-from-users", json=body_no_amount, headers=HEADERS)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "GW_400_VALIDATION"
    assert "missing txnAmount" in r.json()["error"]["message"]


def test_collect_invalid_amount_str_400(client: TestClient) -> None:
    """txnAmount 无法解析为 Decimal → 400 GW_400_VALIDATION。"""
    body_bad = {**COLLECT_BODY, "txnAmount": "not-a-number"}
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


def test_collect_same_key_different_payload_422(client: TestClient) -> None:
    """同 Idempotency-Key 不同 payload（金额变化）→ payload hash 变化 → 422（v2.2 §9.1）。

    §9.1 全 6 个写原语的横向一致性由 tests/api/test_idempotency_payload_mismatch.py 守；
    本条是端点局部回归。
    """
    client.post("/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS)
    mutated = {
        **COLLECT_BODY,
        "txnAmount": "999.0000",
        "userList": [{"userId": "U1", "amount": "999.0000"}],
    }
    r = client.post("/api/v1/bank-funds/collect-from-users", json=mutated, headers=HEADERS)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "GW_422_IDEMPOTENCY_PAYLOAD_MISMATCH"


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


def test_distribute_same_key_different_payload_422(client: TestClient) -> None:
    """distribute 同 Idempotency-Key 不同 payload → 422（v2.2 §9.1）。
    覆盖 bank_funds._submit 内的 IdempotencyRejection 分支。
    """
    h = {**HEADERS, "Idempotency-Key": DISTRIBUTE_BODY["bizSeqNo"], "X-Request-Id": "req-dst2"}
    client.post("/api/v1/bank-funds/distribute-to-users", json=DISTRIBUTE_BODY, headers=h)
    mutated = {
        **DISTRIBUTE_BODY,
        "recipients": [{**DISTRIBUTE_BODY["recipients"][0], "distributeAmount": "999.0000"}],
    }
    r = client.post("/api/v1/bank-funds/distribute-to-users", json=mutated, headers=h)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "GW_422_IDEMPOTENCY_PAYLOAD_MISMATCH"


# ── 缺省 userList（wedap 真形态 body）修复验证 ───────────────────────────────────


def test_collect_without_userlist_passes_validation(client: TestClient) -> None:
    """wedap 真契约：user-collections 不含 userList 字段 → 校验跳过，200 受理。
    修复前：default_factory=list 物化 [] → validate_detail_consistency 误判 empty → 400。
    """
    body = {k: v for k, v in COLLECT_BODY.items() if k != "userList"} | {
        "bizSeqNo": "CLT-20260611-0002000000001",
        "txnAmount": "500.0000",
    }
    h = {**HEADERS, "Idempotency-Key": body["bizSeqNo"], "X-Request-Id": "req-no-ul"}
    r = client.post("/api/v1/bank-funds/collect-from-users", json=body, headers=h)
    assert r.status_code == 200, r.json()


def test_collect_without_userlist_payload_excludes_key(client: TestClient) -> None:
    """缺 userList 时，透传给 wedap 的 payload 不含 userList 键（契约 C：不注入伪字段）。"""
    body = {k: v for k, v in COLLECT_BODY.items() if k != "userList"} | {
        "bizSeqNo": "CLT-20260611-0002000000002",
        "txnAmount": "300.0000",
    }
    h = {**HEADERS, "Idempotency-Key": body["bizSeqNo"], "X-Request-Id": "req-excl"}
    client.post("/api/v1/bank-funds/collect-from-users", json=body, headers=h)
    wedap_mock = client.app.state.wedap  # type: ignore[union-attr]
    # 取 submit_order 实际使用的 payload（通过 SubmitRequest.wedap_payload）
    call_args_str = str(wedap_mock.collect_from_users.call_args)
    assert "userList" not in call_args_str, (
        f"wedap payload 不应含 userList，实际参数：{call_args_str}"
    )


def test_collect_wedap_flat_txnamount_passthrough(client: TestClient) -> None:
    """归集对齐 wedap 扁平：顶层 txnAmount 取金额，bankAccountName 等 extra=allow 透传。"""
    body = {
        "bizSeqNo": "CLT-20260611-0002000000003",
        "channelId": "LEN",
        "transType": "BANK_FUND_COLLECT",
        "bankAccountNo": "ESCROW001",
        "bankAccountName": "P2P ESCROW ACCOUNT",
        "userId": "U1",
        "custAccountNo": "1001234567890",
        "txnAmount": "500.0000",
        "currencyCode": "USD",
    }
    h = {**HEADERS, "Idempotency-Key": body["bizSeqNo"], "X-Request-Id": "req-flat"}
    r = client.post("/api/v1/bank-funds/collect-from-users", json=body, headers=h)
    assert r.status_code == 200, r.json()
    call_str = str(client.app.state.wedap.collect_from_users.call_args)  # type: ignore[union-attr]
    assert "bankAccountName" in call_str and "txnAmount" in call_str


def test_collect_stray_totalamount_never_reaches_wedap(client: TestClient) -> None:
    """误传的旧字段 totalAmount 绝不透传给 wedap（金额只认 txnAmount）。

    2026-08-10 删掉 totalAmount 回退后，它不再是本端点的合法入参；但 CollectRequest 是
    extra=allow 薄透传，上游若仍误传会经 model_dump 漏进 payload → wedap §3.8 未定义字段
    严格校验直接 400 拒收。本例钉死端点侧的无条件剪除。
    """
    body = {k: v for k, v in COLLECT_BODY.items() if k != "userList"} | {
        "bizSeqNo": "CLT-20260611-0002000000005",
        "txnAmount": "777.0000",
        "totalAmount": "500.0000",
    }
    h = {**HEADERS, "Idempotency-Key": body["bizSeqNo"], "X-Request-Id": "req-both"}
    r = client.post("/api/v1/bank-funds/collect-from-users", json=body, headers=h)
    assert r.status_code == 200, r.json()
    call_str = str(client.app.state.wedap.collect_from_users.call_args)  # type: ignore[union-attr]
    assert "777.0000" in call_str
    assert "totalAmount" not in call_str and "500.0000" not in call_str


def test_collect_empty_txnamount_falls_back_or_400(client: TestClient) -> None:
    """txnAmount 空串 → 400 missing（C1：空串视为缺，不报误导的 bad amount）。"""
    body = {
        "bizSeqNo": "CLT-20260611-0002000000006",
        "transType": "LOAN_COLLECT",
        "txnAmount": "",
        "currencyCode": "USD",
    }
    h = {**HEADERS, "Idempotency-Key": body["bizSeqNo"], "X-Request-Id": "req-empty-txn"}
    r = client.post("/api/v1/bank-funds/collect-from-users", json=body, headers=h)
    assert r.status_code == 400
    assert "missing txnAmount" in r.json()["error"]["message"]


def test_distribute_without_recipients_400(client: TestClient) -> None:
    """distribute-to-users 缺 recipients → 400（2026-07-17 决策①方案乙）。

    原「跳过校验 200 受理」会让金额 Σ=0 落 bank_txn_order 台账锚点并污染
    refund 全额护栏基准（R3-DST-07171057 实锤）；wedap 契约无「自动分配」语义。"""
    body = {
        "bizSeqNo": "DST-20260611-0002000000004",
        "transType": "BANK_FUND_DISTRIBUTE",
        "currencyCode": "USD",
    }
    h = {**HEADERS, "Idempotency-Key": body["bizSeqNo"], "X-Request-Id": "req-dst-no-rcp"}
    r = client.post("/api/v1/bank-funds/distribute-to-users", json=body, headers=h)
    assert r.status_code == 400, r.json()
    assert "recipients" in r.json()["error"]["message"]


def test_distribute_amount_summed_and_recipients_passthrough(client: TestClient) -> None:
    """分发金额 = Σ recipients[].distributeAmount；recipients+bankAccountNo 经 extra=allow 透传。"""
    body = {
        **DISTRIBUTE_BODY,
        "bizSeqNo": "DST-20260611-0002000000010",
        "recipients": [
            {
                **DISTRIBUTE_BODY["recipients"][0],
                "userId": "U1",
                "distributeAmount": "60.0000",
                "custAccountNo": "ACC1",
            },
            {
                **DISTRIBUTE_BODY["recipients"][0],
                "userId": "U2",
                "distributeAmount": "40.0000",
                "custAccountNo": "ACC2",
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
        "transType": "BANK_FUND_DISTRIBUTE",
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
        "transType": "BANK_FUND_DISTRIBUTE",
        "currencyCode": "USD",
        "recipients": [],
    }
    h = {**HEADERS, "Idempotency-Key": body["bizSeqNo"], "X-Request-Id": "req-empty-rcp"}
    r = client.post("/api/v1/bank-funds/distribute-to-users", json=body, headers=h)
    assert r.status_code == 400
    assert "recipients" in r.json()["error"]["message"]


def test_distribute_recipient_without_amount_400(client: TestClient) -> None:
    """recipient 缺 distributeAmount → 400（决策①方案乙：金额必填，缺省≠自动分配）。"""
    body = {
        **DISTRIBUTE_BODY,
        "bizSeqNo": "DST-20260611-0002000000013",
        "recipients": [{"userId": "U1", "currencyCode": "USD"}],
    }
    h = {**HEADERS, "Idempotency-Key": body["bizSeqNo"], "X-Request-Id": "req-no-amt"}
    r = client.post("/api/v1/bank-funds/distribute-to-users", json=body, headers=h)
    assert r.status_code == 400, r.json()
    assert "recipients[0].distributeAmount" in r.json()["error"]["message"]


def test_distribute_recipient_null_amount_400(client: TestClient) -> None:
    """recipient distributeAmount=null 与缺省键一致 → 400（决策①方案乙）。

    历史演化：e480f55 曾把 null 归一为「自动分配跳过求和」防 str(None) 误炸；
    2026-07-17 审计定论该语义在 wedap 契约中不存在，缺省/null 一律显式拒收。"""
    body = {
        **DISTRIBUTE_BODY,
        "bizSeqNo": "DST-20260611-0002000000014",
        "recipients": [{"userId": "U1", "distributeAmount": None, "currencyCode": "USD"}],
    }
    h = {**HEADERS, "Idempotency-Key": body["bizSeqNo"], "X-Request-Id": "req-null-amt"}
    r = client.post("/api/v1/bank-funds/distribute-to-users", json=body, headers=h)
    assert r.status_code == 400, r.json()
    assert "recipients[0].distributeAmount" in r.json()["error"]["message"]


# ── 提交响应最小字段契约（SubmitAck · 2026-07-22）──────────────────────────────


def test_collect_response_order_status_submitted(client: TestClient) -> None:
    """契约锁定：wedap PROCESSING → data 含 orderStatus=SUBMITTED；
    None 可选字段（errorCode/errorMsg/inFlight）序列化省略，不出现 null 噪声。"""
    r = client.post("/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS)
    data = r.json()["data"]
    assert data["orderStatus"] == "SUBMITTED"
    # 旧字段不变（纯增量）
    assert data["txnStatus"] == "PROCESSING"
    assert data["bizSeqNo"] == COLLECT_BODY["bizSeqNo"]
    for absent in ("errorCode", "errorMsg", "inFlight"):
        assert absent not in data


def test_collect_sync_success_order_status_succeeded(client: TestClient) -> None:
    """wedap 同步终态 SUCCESS → orderStatus=SUCCEEDED（受理即落账，调用方免查单）。"""
    client.app.state.wedap.collect_from_users.return_value = {"txnStatus": "SUCCESS"}  # type: ignore[union-attr]
    body = {**COLLECT_BODY, "bizSeqNo": "CLT-20260611-0009000000001"}
    h = {**HEADERS, "Idempotency-Key": body["bizSeqNo"], "X-Request-Id": "req-sync-ok"}
    r = client.post("/api/v1/bank-funds/collect-from-users", json=body, headers=h)
    data = r.json()["data"]
    assert data["txnStatus"] == "SUCCESS" and data["orderStatus"] == "SUCCEEDED"


def test_collect_replay_returns_same_order_status(client: TestClient) -> None:
    """幂等重放：first_response 冻结（含 orderStatus），重放与首次一致且零外呼。"""
    r1 = client.post("/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS)
    r2 = client.post("/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS)
    assert r1.json()["data"] == r2.json()["data"]
    assert r2.json()["data"]["orderStatus"] == "SUBMITTED"
    assert client.app.state.wedap.collect_from_users.await_count == 1  # type: ignore[union-attr]


def test_legacy_replay_without_order_status_key(client: TestClient) -> None:
    """契约上线前受理单的重放：first_response 无 orderStatus → 不回填、键不出现
    （幂等铁律：重放逐字节等于首次；orderStatus 以查单为准）。"""
    import asyncio as _asyncio

    from sqlalchemy import select, update

    from app.models.idempotency import IdempotencyRecord

    client.post("/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS)

    async def _strip() -> None:
        factory = client.app.state.session_factory  # type: ignore[union-attr]
        async with factory() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(IdempotencyRecord).where(
                            IdempotencyRecord.idempotency_key == COLLECT_BODY["bizSeqNo"]
                        )
                    )
                ).scalar_one()
                legacy = {k: v for k, v in row.first_response.items() if k != "orderStatus"}
                await session.execute(
                    update(IdempotencyRecord)
                    .where(IdempotencyRecord.id == row.id)
                    .values(first_response=legacy)
                )

    _asyncio.run(_strip())
    r = client.post("/api/v1/bank-funds/collect-from-users", json=COLLECT_BODY, headers=HEADERS)
    data = r.json()["data"]
    assert "orderStatus" not in data
    assert data["txnStatus"] == "PROCESSING" and data["bizSeqNo"] == COLLECT_BODY["bizSeqNo"]


def test_submit_endpoints_openapi_declare_submit_ack(client: TestClient) -> None:
    """openapi 契约：四写原语 200 响应引用 SubmitAckEnvelope（不再是无字段 object）。"""
    spec = client.get("/openapi.json", headers={"X-Caller-Service": "test-runner"}).json()
    for path in (
        "/api/v1/bank-funds/collect-from-users",
        "/api/v1/bank-funds/distribute-to-users",
        "/api/v1/bank-funds/refunds",
        "/api/v1/bank-funds/reversals",
    ):
        ref = spec["paths"][path]["post"]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]["$ref"]
        assert ref.endswith("/SubmitAckEnvelope"), (path, ref)
    props = spec["components"]["schemas"]["SubmitAck"]["properties"]
    assert {"bizSeqNo", "txnStatus", "orderStatus", "errorCode", "errorMsg", "inFlight"} <= set(
        props
    )
