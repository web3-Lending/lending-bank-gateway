import json
import pathlib

import httpx
import pytest
import respx

from app.clients.wedap import WedapClient, WedapError

FIX = pathlib.Path(__file__).parent.parent / "fixtures" / "wedap"

BASE = "http://wedap"


def _client() -> WedapClient:
    return WedapClient(base_url=BASE, timeout_seconds=1.0)


# ---------------------------------------------------------------------------
# submit_disbursement
# ---------------------------------------------------------------------------


@respx.mock
async def test_submit_disbursement_accepted() -> None:
    route = respx.post(f"{BASE}/api/v1/loans/p2p-disbursements").mock(
        return_value=httpx.Response(
            200, json=json.loads((FIX / "disbursement_accepted.json").read_text())
        )
    )
    resp = await _client().submit_disbursement(
        tenant_id="OCBC",
        request_id="req-0001",
        payload={"bizSeqNo": "DSB-20260611-0001234567890"},
    )
    assert resp["txnStatus"] == "PROCESSING"
    assert route.called
    req = route.calls.last.request
    assert req.headers["X-Tenant-Id"] == "OCBC"
    assert req.headers["X-Request-Id"] == "req-0001"


@respx.mock
async def test_submit_disbursement_non_200_code_raises() -> None:
    respx.post(f"{BASE}/api/v1/loans/p2p-disbursements").mock(
        return_value=httpx.Response(200, json={"code": "500", "msg": "SYSTEM_ERROR"})
    )
    with pytest.raises(WedapError) as exc_info:
        await _client().submit_disbursement(tenant_id="OCBC", request_id="r", payload={})
    assert exc_info.value.code == "500"


@respx.mock
async def test_submit_disbursement_http_error_raises() -> None:
    respx.post(f"{BASE}/api/v1/loans/p2p-disbursements").mock(
        return_value=httpx.Response(502, text="Bad Gateway")
    )
    with pytest.raises(httpx.HTTPStatusError):
        await _client().submit_disbursement(tenant_id="OCBC", request_id="r", payload={})


@respx.mock
async def test_submit_disbursement_timeout_propagates() -> None:
    respx.post(f"{BASE}/api/v1/loans/p2p-disbursements").mock(side_effect=httpx.ConnectTimeout("t"))
    with pytest.raises(httpx.TimeoutException):
        await _client().submit_disbursement(tenant_id="OCBC", request_id="r", payload={})


@respx.mock
async def test_submit_disbursement_missing_data_returns_empty() -> None:
    respx.post(f"{BASE}/api/v1/loans/p2p-disbursements").mock(
        return_value=httpx.Response(200, json={"code": "200", "msg": "SUCCESS"})
    )
    resp = await _client().submit_disbursement(tenant_id="OCBC", request_id="r", payload={})
    assert resp == {}


# ---------------------------------------------------------------------------
# get_composite_steps
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_composite_steps() -> None:
    biz_seq_no = "DSB-20260611-0001234567890"
    route = respx.get(f"{BASE}/api/v1/composite-transactions/{biz_seq_no}/steps").mock(
        return_value=httpx.Response(200, json=json.loads((FIX / "steps_two_legs.json").read_text()))
    )
    steps = await _client().get_composite_steps(tenant_id="OCBC", biz_seq_no=biz_seq_no)
    assert len(steps) == 2
    assert steps[0]["sysRefNo"] == "HSBC202606110001"
    assert route.called
    req = route.calls.last.request
    assert req.headers["X-Tenant-Id"] == "OCBC"


@respx.mock
async def test_get_composite_steps_missing_steps_returns_empty() -> None:
    biz_seq_no = "DSB-20260611-0001234567890"
    respx.get(f"{BASE}/api/v1/composite-transactions/{biz_seq_no}/steps").mock(
        return_value=httpx.Response(200, json={"code": "200", "msg": "SUCCESS", "data": {}})
    )
    steps = await _client().get_composite_steps(tenant_id="OCBC", biz_seq_no=biz_seq_no)
    assert steps == []


# ---------------------------------------------------------------------------
# submit_repayment
# ---------------------------------------------------------------------------


@respx.mock
async def test_submit_repayment_happy_path() -> None:
    route = respx.post(f"{BASE}/api/v1/loans/p2p-repayments").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": "200",
                "msg": "SUCCESS",
                "data": {"txnStatus": "PROCESSING", "bizSeqNo": "REP-001"},
            },
        )
    )
    resp = await _client().submit_repayment(
        tenant_id="OCBC", request_id="rep-req-001", payload={"bizSeqNo": "REP-001"}
    )
    assert resp["txnStatus"] == "PROCESSING"
    req = route.calls.last.request
    assert req.headers["X-Tenant-Id"] == "OCBC"
    assert req.headers["X-Request-Id"] == "rep-req-001"


@respx.mock
async def test_submit_repayment_non_200_code_raises() -> None:
    respx.post(f"{BASE}/api/v1/loans/p2p-repayments").mock(
        return_value=httpx.Response(200, json={"code": "400", "msg": "INVALID_PARAM"})
    )
    with pytest.raises(WedapError):
        await _client().submit_repayment(tenant_id="OCBC", request_id="r", payload={})


@respx.mock
async def test_submit_repayment_missing_data_returns_empty() -> None:
    respx.post(f"{BASE}/api/v1/loans/p2p-repayments").mock(
        return_value=httpx.Response(200, json={"code": "200", "msg": "SUCCESS"})
    )
    resp = await _client().submit_repayment(tenant_id="OCBC", request_id="r", payload={})
    assert resp == {}


# ---------------------------------------------------------------------------
# collect_from_users
# ---------------------------------------------------------------------------


@respx.mock
async def test_collect_from_users_happy_path() -> None:
    route = respx.post(f"{BASE}/api/v1/bank-funds/user-collections").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": "200",
                "msg": "SUCCESS",
                "data": {"txnStatus": "PROCESSING", "bizSeqNo": "COL-001"},
            },
        )
    )
    resp = await _client().collect_from_users(
        tenant_id="OCBC", request_id="col-req-001", payload={"amount": "100.0000"}
    )
    assert resp["bizSeqNo"] == "COL-001"
    req = route.calls.last.request
    assert req.headers["X-Tenant-Id"] == "OCBC"
    assert req.headers["X-Request-Id"] == "col-req-001"


@respx.mock
async def test_collect_from_users_non_200_code_raises() -> None:
    respx.post(f"{BASE}/api/v1/bank-funds/user-collections").mock(
        return_value=httpx.Response(200, json={"code": "500", "msg": "SYSTEM_ERROR"})
    )
    with pytest.raises(WedapError):
        await _client().collect_from_users(tenant_id="OCBC", request_id="r", payload={})


@respx.mock
async def test_collect_from_users_missing_data_returns_empty() -> None:
    respx.post(f"{BASE}/api/v1/bank-funds/user-collections").mock(
        return_value=httpx.Response(200, json={"code": "200", "msg": "SUCCESS"})
    )
    resp = await _client().collect_from_users(tenant_id="OCBC", request_id="r", payload={})
    assert resp == {}


# ---------------------------------------------------------------------------
# distribute_to_users
# ---------------------------------------------------------------------------


@respx.mock
async def test_distribute_to_users_happy_path() -> None:
    route = respx.post(f"{BASE}/api/v1/bank-funds/user-distributions").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": "200",
                "msg": "SUCCESS",
                "data": {"txnStatus": "PROCESSING", "bizSeqNo": "DIST-001"},
            },
        )
    )
    resp = await _client().distribute_to_users(
        tenant_id="OCBC", request_id="dist-req-001", payload={"amount": "100.0000"}
    )
    assert resp["bizSeqNo"] == "DIST-001"
    req = route.calls.last.request
    assert req.headers["X-Tenant-Id"] == "OCBC"
    assert req.headers["X-Request-Id"] == "dist-req-001"


@respx.mock
async def test_distribute_to_users_non_200_code_raises() -> None:
    respx.post(f"{BASE}/api/v1/bank-funds/user-distributions").mock(
        return_value=httpx.Response(200, json={"code": "404", "msg": "NOT_FOUND"})
    )
    with pytest.raises(WedapError):
        await _client().distribute_to_users(tenant_id="OCBC", request_id="r", payload={})


@respx.mock
async def test_distribute_to_users_missing_data_returns_empty() -> None:
    respx.post(f"{BASE}/api/v1/bank-funds/user-distributions").mock(
        return_value=httpx.Response(200, json={"code": "200", "msg": "SUCCESS"})
    )
    resp = await _client().distribute_to_users(tenant_id="OCBC", request_id="r", payload={})
    assert resp == {}


# ---------------------------------------------------------------------------
# query_funds_status（biz_type 感知，wedap 无统一状态接口）
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_funds_status_dsb_routes_to_disbursement_status() -> None:
    """DSB → GET /api/v1/loans/p2p-disbursements/{biz}/status。"""
    biz = "DSB-20260612-0001"
    route = respx.get(f"{BASE}/api/v1/loans/p2p-disbursements/{biz}/status").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": "200",
                "msg": "SUCCESS",
                "data": {"txnStatus": "SUCCESS", "bizSeqNo": biz},
            },
        )
    )
    resp = await _client().query_funds_status(
        tenant_id="OCBC", request_id="qry-dsb-001", biz_seq_no=biz, biz_type="DSB"
    )
    assert resp["txnStatus"] == "SUCCESS"
    req = route.calls.last.request
    assert req.headers["X-Tenant-Id"] == "OCBC"
    assert req.headers["X-Request-Id"] == "qry-dsb-001"


@respx.mock
async def test_query_funds_status_rpy_routes_to_repayment_status() -> None:
    """RPY → GET /api/v1/loans/p2p-repayments/{biz}/status。"""
    biz = "RPY-20260612-0001"
    route = respx.get(f"{BASE}/api/v1/loans/p2p-repayments/{biz}/status").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": "200",
                "msg": "SUCCESS",
                "data": {"txnStatus": "PROCESSING", "bizSeqNo": biz},
            },
        )
    )
    resp = await _client().query_funds_status(
        tenant_id="OCBC", request_id="qry-rpy-001", biz_seq_no=biz, biz_type="RPY"
    )
    assert resp["txnStatus"] == "PROCESSING"
    req = route.calls.last.request
    assert req.headers["X-Tenant-Id"] == "OCBC"


@respx.mock
async def test_query_funds_status_dst_routes_to_user_distributions() -> None:
    """DST → GET /api/v1/bank-funds/user-distributions/{biz}。"""
    biz = "DST-20260612-0001"
    route = respx.get(f"{BASE}/api/v1/bank-funds/user-distributions/{biz}").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": "200",
                "msg": "SUCCESS",
                "data": {"txnStatus": "SUCCESS", "bizSeqNo": biz},
            },
        )
    )
    resp = await _client().query_funds_status(
        tenant_id="OCBC", request_id="qry-dst-001", biz_seq_no=biz, biz_type="DST"
    )
    assert resp["txnStatus"] == "SUCCESS"
    req = route.calls.last.request
    assert req.headers["X-Tenant-Id"] == "OCBC"


async def test_query_funds_status_unsupported_biz_type_raises() -> None:
    """CLT 等无状态接口的类型 → WedapError(UNSUPPORTED)。"""
    with pytest.raises(WedapError) as exc_info:
        await _client().query_funds_status(
            tenant_id="OCBC", request_id="r", biz_seq_no="CLT-001", biz_type="CLT"
        )
    assert exc_info.value.code == "UNSUPPORTED"
    assert "CLT" in str(exc_info.value)


@respx.mock
async def test_query_funds_status_dsb_missing_data_returns_empty() -> None:
    """wedap 返回 code=200 但 data 为空 → 返回 {}。"""
    biz = "DSB-20260612-0002"
    respx.get(f"{BASE}/api/v1/loans/p2p-disbursements/{biz}/status").mock(
        return_value=httpx.Response(200, json={"code": "200", "msg": "SUCCESS"})
    )
    resp = await _client().query_funds_status(
        tenant_id="OCBC", request_id="r", biz_seq_no=biz, biz_type="DSB"
    )
    assert resp == {}


# ---------------------------------------------------------------------------
# get_deposit_balance_total
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_deposit_balance_total_happy_path() -> None:
    route = respx.get(f"{BASE}/api/v1/deposit/balances/total").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": "200",
                "msg": "SUCCESS",
                "data": {"totalBalance": "1000.0000", "currencyCode": "USD"},
            },
        )
    )
    resp = await _client().get_deposit_balance_total(
        tenant_id="OCBC", request_id="bal-001", params={"userId": "U001"}
    )
    assert resp["totalBalance"] == "1000.0000"
    req = route.calls.last.request
    assert req.headers["X-Tenant-Id"] == "OCBC"
    assert req.headers["X-Request-Id"] == "bal-001"
    assert "userId=U001" in str(req.url)


@respx.mock
async def test_get_deposit_balance_total_non_200_code_raises() -> None:
    respx.get(f"{BASE}/api/v1/deposit/balances/total").mock(
        return_value=httpx.Response(200, json={"code": "404", "msg": "USER_NOT_FOUND"})
    )
    with pytest.raises(WedapError):
        await _client().get_deposit_balance_total(
            tenant_id="OCBC", request_id="r", params={"userId": "U999"}
        )


@respx.mock
async def test_get_deposit_balance_total_missing_data_returns_empty() -> None:
    respx.get(f"{BASE}/api/v1/deposit/balances/total").mock(
        return_value=httpx.Response(200, json={"code": "200", "msg": "SUCCESS"})
    )
    resp = await _client().get_deposit_balance_total(
        tenant_id="OCBC", request_id="r", params={"userId": "U001"}
    )
    assert resp == {}


# ---------------------------------------------------------------------------
# get_deposit_accounts
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_deposit_accounts_happy_path() -> None:
    route = respx.get(f"{BASE}/api/v1/deposit/accounts").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": "200",
                "msg": "SUCCESS",
                "data": {"accounts": [{"accountId": "ACC001", "currencyCode": "USD"}]},
            },
        )
    )
    resp = await _client().get_deposit_accounts(
        tenant_id="OCBC", request_id="acc-001", params={"userId": "U001"}
    )
    assert "accounts" in resp
    req = route.calls.last.request
    assert req.headers["X-Tenant-Id"] == "OCBC"
    assert req.headers["X-Request-Id"] == "acc-001"
    assert "userId=U001" in str(req.url)


@respx.mock
async def test_get_deposit_accounts_non_200_code_raises() -> None:
    respx.get(f"{BASE}/api/v1/deposit/accounts").mock(
        return_value=httpx.Response(200, json={"code": "500", "msg": "SYSTEM_ERROR"})
    )
    with pytest.raises(WedapError):
        await _client().get_deposit_accounts(
            tenant_id="OCBC", request_id="r", params={"userId": "U999"}
        )


@respx.mock
async def test_get_deposit_accounts_missing_data_returns_empty() -> None:
    respx.get(f"{BASE}/api/v1/deposit/accounts").mock(
        return_value=httpx.Response(200, json={"code": "200", "msg": "SUCCESS"})
    )
    resp = await _client().get_deposit_accounts(
        tenant_id="OCBC", request_id="r", params={"userId": "U001"}
    )
    assert resp == {}


# ---------------------------------------------------------------------------
# get_user_info
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_user_info_happy_path() -> None:
    route = respx.get(f"{BASE}/api/v1/users/info").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": "200",
                "msg": "SUCCESS",
                "data": {"userId": "U001", "name": "Alice"},
            },
        )
    )
    resp = await _client().get_user_info(
        tenant_id="OCBC",
        request_id="usr-001",
        params={"userId": "U001"},
    )
    assert resp["userId"] == "U001"
    req = route.calls.last.request
    assert req.headers["X-Tenant-Id"] == "OCBC"
    assert req.headers["X-Request-Id"] == "usr-001"
    assert "userId=U001" in str(req.url)


@respx.mock
async def test_get_user_info_non_200_code_raises() -> None:
    respx.get(f"{BASE}/api/v1/users/info").mock(
        return_value=httpx.Response(200, json={"code": "404", "msg": "USER_NOT_FOUND"})
    )
    with pytest.raises(WedapError):
        await _client().get_user_info(tenant_id="OCBC", request_id="r", params={"userId": "U999"})


@respx.mock
async def test_get_user_info_missing_data_returns_empty() -> None:
    respx.get(f"{BASE}/api/v1/users/info").mock(
        return_value=httpx.Response(200, json={"code": "200", "msg": "SUCCESS"})
    )
    resp = await _client().get_user_info(
        tenant_id="OCBC", request_id="r", params={"userId": "U001"}
    )
    assert resp == {}
