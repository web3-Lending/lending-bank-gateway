"""wedap 必填集 / 禁止字段的入口 fail-fast 校验（2026-08-11 实测契约）。

这批用例的存在理由：既有 `COLLECT_BODY` 等 fixture 用的是 wedap 契约里**根本不存在**的
字段（`userList`），所以「测试全绿」从来证明不了「真报文能被 wedap 接受」。本文件的
基线报文一律对齐 2026-08-11 dev 实测**通过 wedap schema 校验**的形态。

断言只对照 `app.domain.wedap_contract` 的契约真源，不写死字段名字面量——
契约变更时改真源即可，用例自动跟随。
"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.domain.wedap_contract import (
    COLLECT_REJECTED,
    COLLECT_REQUIRED,
    DISTRIBUTE_RECIPIENT_REJECTED,
    DISTRIBUTE_RECIPIENT_REQUIRED,
    DISTRIBUTE_REQUIRED,
)
from app.main import create_app
from app.models.base import Base

HEADERS = {
    "X-Caller-Service": "lifecycle",
    "X-Tenant-Id": "OCBC",
    "X-Request-Id": "req-req-1",
}

# 2026-08-11 实测形态：扁平 userId + 顶层账户三件套（I1/I5 均过 wedap schema 校验）
REAL_COLLECT = {
    "bizSeqNo": "CLT-20260811-0000000000001",
    "currencyCode": "USD",
    "transType": "BANK_FUND_COLLECT_LOAN",
    "txnAmount": "1.0000",
    "channelId": "LEN",
    "userId": "09966101774108",
    "custAccountNo": "12348401030101002088",
    "bankAccountNo": "12348401030101002088",
    "bankAccountName": "TEST LENDER ACCOUNT",
}
# 2026-08-11 实测形态：付款方在顶层、收款人在 recipients[]（H1/H3 过 schema 校验）
REAL_DISTRIBUTE = {
    "bizSeqNo": "DST-20260811-0000000000001",
    "currencyCode": "USD",
    "transType": "BANK_FUND_DISTRIBUTE",
    "channelId": "LEN",
    "bankAccountNo": "9001020000000001",
    "bankAccountName": "PLATFORM COLLECTION ACCOUNT",
    "recipients": [
        {
            "userId": "09966101774108",
            "userName": "TEST LENDER ACCOUNT",
            "currencyCode": "USD",
            "custAccountNo": "12348401030101002088",
            "distributeAmount": "1.0000",
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


def _post(client: TestClient, path: str, body: dict) -> tuple[int, dict]:
    r = client.post(
        f"/api/v1/bank-funds/{path}",
        json=body,
        headers={**HEADERS, "Idempotency-Key": body["bizSeqNo"]},
    )
    return r.status_code, r.json()


# ── collect ───────────────────────────────────────────────────────────────────


def test_real_shape_collect_is_accepted(client: TestClient) -> None:
    """回归护栏：实测通过 wedap schema 的报文必须仍被放行（防校验写过严）。"""
    status, body = _post(client, "collect-from-users", REAL_COLLECT)
    assert status == 200, body
    assert body["success"] is True


@pytest.mark.parametrize("field", COLLECT_REQUIRED)
def test_collect_missing_required_field_rejected(client: TestClient, field: str) -> None:
    """逐个删 wedap 必填字段 → gateway 400，且文案点名该字段（不打到 wedap、不落 FAILED 单）。"""
    body = {k: v for k, v in REAL_COLLECT.items() if k != field}
    status, resp = _post(client, "collect-from-users", body)
    assert status == 400
    assert resp["error"]["code"] == "GW_400_VALIDATION"
    assert field in resp["error"]["message"]


def test_collect_missing_fields_are_reported_together(client: TestClient) -> None:
    """一次列全所有缺失字段——学 wedap 的做法，省掉上游「改一个再试一次」的往返。"""
    body = {k: v for k, v in REAL_COLLECT.items() if k not in ("bankAccountName", "channelId")}
    status, resp = _post(client, "collect-from-users", body)
    assert status == 400
    assert "bankAccountName" in resp["error"]["message"]
    assert "channelId" in resp["error"]["message"]


def test_collect_blank_required_field_rejected(client: TestClient) -> None:
    """空串等同缺失：wedap 的判据是 `must not be blank`，不是「键存在」。"""
    status, resp = _post(client, "collect-from-users", {**REAL_COLLECT, "bankAccountName": "  "})
    assert status == 400
    assert "bankAccountName" in resp["error"]["message"]


def test_collect_nested_user_object_rejected_with_migration_hint(client: TestClient) -> None:
    """嵌套 user{} 被 wedap 整包拒 → gateway 提前拒，且给出改扁平的指引。

    不静默剪掉：userId/custAccountNo 等必填值就在 user{} 里，剪掉会把「结构不对」
    伪装成「字段缺失」，上游按报错去补顶层字段仍然会错第二次。
    """
    body = {
        "bizSeqNo": REAL_COLLECT["bizSeqNo"],
        "currencyCode": "USD",
        "transType": "BANK_FUND_COLLECT_LOAN",
        "txnAmount": "1.0000",
        "channelId": "LEN",
        "bankAccountNo": "12348401030101002088",
        "bankAccountName": "TEST LENDER ACCOUNT",
        "user": {"userId": "09966101774108", "custAccountNo": "12348401030101002088"},
    }
    status, resp = _post(client, "collect-from-users", body)
    assert status == 400
    assert resp["error"]["code"] == "GW_400_VALIDATION"
    assert "user" in resp["error"]["message"]
    assert COLLECT_REJECTED["user"] in resp["error"]["message"]


# ── distribute ────────────────────────────────────────────────────────────────


def test_real_shape_distribute_is_accepted(client: TestClient) -> None:
    """回归护栏：实测形态的分发报文必须仍被放行。"""
    status, body = _post(client, "distribute-to-users", REAL_DISTRIBUTE)
    assert status == 200, body
    assert body["success"] is True


@pytest.mark.parametrize("field", DISTRIBUTE_REQUIRED)
def test_distribute_missing_top_level_required_rejected(client: TestClient, field: str) -> None:
    """顶层付款方字段（平台户）缺失 → 400。"""
    body = {k: v for k, v in REAL_DISTRIBUTE.items() if k != field}
    status, resp = _post(client, "distribute-to-users", body)
    assert status == 400
    assert field in resp["error"]["message"]


@pytest.mark.parametrize("field", DISTRIBUTE_RECIPIENT_REQUIRED)
def test_distribute_recipient_missing_required_rejected(client: TestClient, field: str) -> None:
    """recipients[] 内必填缺失 → 400，且带下标定位（多收款人时能指到具体一项）。"""
    rcpt = {k: v for k, v in REAL_DISTRIBUTE["recipients"][0].items() if k != field}
    body = {**REAL_DISTRIBUTE, "recipients": [rcpt]}
    status, resp = _post(client, "distribute-to-users", body)
    assert status == 400
    assert field in resp["error"]["message"]
    assert "recipients[0]" in resp["error"]["message"]


@pytest.mark.parametrize("field", sorted(DISTRIBUTE_RECIPIENT_REJECTED))
def test_distribute_recipient_account_fields_rejected(client: TestClient, field: str) -> None:
    """账户身份字段出现在 recipients[] → wedap `Unknown field` 整包拒，gateway 提前挡。"""
    rcpt = {**REAL_DISTRIBUTE["recipients"][0], field: "12348401030101002088"}
    body = {**REAL_DISTRIBUTE, "recipients": [rcpt]}
    status, resp = _post(client, "distribute-to-users", body)
    assert status == 400
    assert field in resp["error"]["message"]
    assert DISTRIBUTE_RECIPIENT_REJECTED[field] in resp["error"]["message"]


def test_distribute_second_recipient_index_reported(client: TestClient) -> None:
    """下标必须指向真正出错的那一项，不能恒报 [0]。"""
    good = REAL_DISTRIBUTE["recipients"][0]
    bad = {k: v for k, v in good.items() if k != "userName"}
    body = {**REAL_DISTRIBUTE, "recipients": [good, bad]}
    status, resp = _post(client, "distribute-to-users", body)
    assert status == 400
    assert "recipients[1]" in resp["error"]["message"]
