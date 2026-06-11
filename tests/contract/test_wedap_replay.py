"""南向契约 replay 测试：fixtures 全量回放钉死 WedapClient 解包逻辑。

每个 client 方法至少一条 replay，确保字段映射、_unwrap、异常路径不因重构漂移。
所有 mock 均通过 respx，不发起真实网络请求。
"""

import json
import pathlib
from typing import Any

import httpx
import pytest
import respx

from app.clients.wedap import WedapClient, WedapError

FIX = pathlib.Path(__file__).parent.parent / "fixtures" / "wedap"

_BASE = "http://wedap-test"
_TENANT = "OCBC"
_REQ_ID = "req-replay-0001"


def _client() -> WedapClient:
    return WedapClient(base_url=_BASE, timeout_seconds=1.0)


def _fix(name: str) -> dict[str, Any]:
    return json.loads((FIX / name).read_text())  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# loans
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_replay_disbursement_fixture() -> None:
    """disbursement_accepted.json：txnStatus + bizSeqNo 解包正确。"""
    body = _fix("disbursement_accepted.json")
    respx.post(f"{_BASE}/api/v1/loans/p2p-disbursements").mock(
        return_value=httpx.Response(200, json=body)
    )
    data = await _client().submit_disbursement(tenant_id=_TENANT, request_id=_REQ_ID, payload={})
    assert data["txnStatus"] == "PROCESSING"
    assert data["bizSeqNo"] == "DSB-20260611-0001234567890"


@pytest.mark.asyncio
@respx.mock
async def test_replay_repayment_fixture() -> None:
    """repayment_accepted.json：repayment 路径解包正确。"""
    body = _fix("repayment_accepted.json")
    respx.post(f"{_BASE}/api/v1/loans/p2p-repayments").mock(
        return_value=httpx.Response(200, json=body)
    )
    data = await _client().submit_repayment(tenant_id=_TENANT, request_id=_REQ_ID, payload={})
    assert data["txnStatus"] == "PROCESSING"
    assert data["bizSeqNo"].startswith("RPY-")


# ---------------------------------------------------------------------------
# bank-funds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_replay_collect_fixture() -> None:
    """collect_accepted.json：collect-from-users 路径解包正确。"""
    body = _fix("collect_accepted.json")
    respx.post(f"{_BASE}/api/v1/bank-funds/collect-from-users").mock(
        return_value=httpx.Response(200, json=body)
    )
    data = await _client().collect_from_users(tenant_id=_TENANT, request_id=_REQ_ID, payload={})
    assert data["txnStatus"] == "PROCESSING"
    assert data["bizSeqNo"].startswith("COL-")


@pytest.mark.asyncio
@respx.mock
async def test_replay_distribute_fixture() -> None:
    """distribute_accepted.json：distribute-to-users 路径解包正确。"""
    body = _fix("distribute_accepted.json")
    respx.post(f"{_BASE}/api/v1/bank-funds/distribute-to-users").mock(
        return_value=httpx.Response(200, json=body)
    )
    data = await _client().distribute_to_users(tenant_id=_TENANT, request_id=_REQ_ID, payload={})
    assert data["txnStatus"] == "PROCESSING"
    assert data["bizSeqNo"].startswith("DIS-")


@pytest.mark.asyncio
@respx.mock
async def test_replay_funds_status_fixture() -> None:
    """funds_status.json：query_funds_status 解包 txnStatus=SUCCESS。"""
    body = _fix("funds_status.json")
    respx.get(f"{_BASE}/api/v1/bank-funds/status").mock(return_value=httpx.Response(200, json=body))
    data = await _client().query_funds_status(
        tenant_id=_TENANT, request_id=_REQ_ID, biz_seq_no="COL-20260611-0001111111111"
    )
    assert data["txnStatus"] == "SUCCESS"
    assert data["bizSeqNo"] == "COL-20260611-0001111111111"


# ---------------------------------------------------------------------------
# composite steps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_replay_steps_fixture() -> None:
    """steps_two_legs.json：两步结构、sysRefNo 顺序、必填字段全覆盖。"""
    body = _fix("steps_two_legs.json")
    biz_seq_no = "DSB-20260611-0001234567890"
    respx.get(f"{_BASE}/api/v1/composite-transactions/{biz_seq_no}/steps").mock(
        return_value=httpx.Response(200, json=body)
    )
    steps = await _client().get_composite_steps(tenant_id=_TENANT, biz_seq_no=biz_seq_no)
    assert [s["sysRefNo"] for s in steps] == ["HSBC202606110001", "HSBC202606110002"]
    assert all({"stepType", "stepSeq", "amount", "status"} <= set(s) for s in steps)


# ---------------------------------------------------------------------------
# deposit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_replay_deposit_balance_total_fixture() -> None:
    """deposit_balance_total.json：accounts 列表 + totalBalance 解包正确。"""
    body = _fix("deposit_balance_total.json")
    respx.get(f"{_BASE}/api/v1/deposit/balances/total").mock(
        return_value=httpx.Response(200, json=body)
    )
    data = await _client().get_deposit_balance_total(
        tenant_id=_TENANT, request_id=_REQ_ID, user_id="U-0001"
    )
    assert data["userId"] == "U-0001"
    assert len(data["accounts"]) == 1
    assert data["accounts"][0]["custAccountNo"] == "ACC-001"


@pytest.mark.asyncio
@respx.mock
async def test_replay_deposit_accounts_fixture() -> None:
    """deposit_accounts.json：账户列表两条记录解包正确。"""
    body = _fix("deposit_accounts.json")
    respx.get(f"{_BASE}/api/v1/deposit/accounts").mock(return_value=httpx.Response(200, json=body))
    data = await _client().get_deposit_accounts(
        tenant_id=_TENANT, request_id=_REQ_ID, user_id="U-0001"
    )
    assert data["userId"] == "U-0001"
    assert len(data["accounts"]) == 2


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_replay_user_info_fixture() -> None:
    """user_info.json：用户信息解包、kycStatus 字段存在。"""
    body = _fix("user_info.json")
    respx.get(f"{_BASE}/api/v1/users/info").mock(return_value=httpx.Response(200, json=body))
    data = await _client().get_user_info(
        tenant_id=_TENANT, request_id=_REQ_ID, params={"userId": "U-0001"}
    )
    assert data["userId"] == "U-0001"
    assert data["kycStatus"] == "VERIFIED"


# ---------------------------------------------------------------------------
# 错误路径：WedapError 解包
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_wedap_error_unwrap() -> None:
    """_unwrap 对非 200 code 应抛出 WedapError，不静默吞掉上游错误。"""
    body = {"code": "4001", "msg": "INSUFFICIENT_FUNDS", "data": None, "timestamp": 0}
    respx.post(f"{_BASE}/api/v1/loans/p2p-disbursements").mock(
        return_value=httpx.Response(200, json=body)
    )
    with pytest.raises(WedapError) as exc_info:
        await _client().submit_disbursement(tenant_id=_TENANT, request_id=_REQ_ID, payload={})
    assert exc_info.value.code == "4001"
