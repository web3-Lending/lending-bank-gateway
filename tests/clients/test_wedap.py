import json
import pathlib

import httpx
import pytest
import respx

from app.clients.wedap import (
    KNOWN_BATCH_STATUS,
    WedapClient,
    WedapError,
    WedapGatewayRejected,
)

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
    """DISB → GET /api/v1/loans/p2p-disbursements/{biz}/status。"""
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
        tenant_id="OCBC", request_id="qry-dsb-001", biz_seq_no=biz, biz_type="DISB"
    )
    assert resp["txnStatus"] == "SUCCESS"
    req = route.calls.last.request
    assert req.headers["X-Tenant-Id"] == "OCBC"
    assert req.headers["X-Request-Id"] == "qry-dsb-001"


@respx.mock
async def test_query_funds_status_rpy_routes_to_repayment_status() -> None:
    """RPMT → GET /api/v1/loans/p2p-repayments/{biz}/status。"""
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
        tenant_id="OCBC", request_id="qry-rpy-001", biz_seq_no=biz, biz_type="RPMT"
    )
    assert resp["txnStatus"] == "PROCESSING"
    req = route.calls.last.request
    assert req.headers["X-Tenant-Id"] == "OCBC"


@respx.mock
async def test_query_funds_status_dst_routes_to_user_distributions() -> None:
    """DIST → GET /api/v1/bank-funds/user-distributions/{biz}。"""
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
        tenant_id="OCBC", request_id="qry-dst-001", biz_seq_no=biz, biz_type="DIST"
    )
    assert resp["txnStatus"] == "SUCCESS"
    req = route.calls.last.request
    assert req.headers["X-Tenant-Id"] == "OCBC"


async def test_query_funds_status_unsupported_biz_type_raises() -> None:
    """COLL(归集) 等无状态接口的类型 → WedapError(UNSUPPORTED)。"""
    with pytest.raises(WedapError) as exc_info:
        await _client().query_funds_status(
            tenant_id="OCBC", request_id="r", biz_seq_no="COLL-001", biz_type="COLL"
        )
    assert exc_info.value.code == "UNSUPPORTED"
    assert "COLL" in str(exc_info.value)


@respx.mock
async def test_query_funds_status_dsb_missing_data_returns_empty() -> None:
    """wedap 返回 code=200 但 data 为空 → 返回 {}。"""
    biz = "DSB-20260612-0002"
    respx.get(f"{BASE}/api/v1/loans/p2p-disbursements/{biz}/status").mock(
        return_value=httpx.Response(200, json={"code": "200", "msg": "SUCCESS"})
    )
    resp = await _client().query_funds_status(
        tenant_id="OCBC", request_id="r", biz_seq_no=biz, biz_type="DISB"
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


# ---------------------------------------------------------------------------
# notify_batch_uploaded (flow-import)
# ---------------------------------------------------------------------------

NOTIFY_PATH = f"{BASE}/bank/api/v1/import/batch-uploaded"


def _import_client() -> WedapClient:
    return WedapClient(base_url=BASE, timeout_seconds=1.0, import_api_key="KEY123")


def _payload() -> dict:
    return {
        "dataType": "interest-accrual",
        "channelId": "LEN",
        "importBatchNo": "BATCH-LEN-20260624-001",
        "importDate": "20260624",
        "fileChecksum": "a" * 64,
        "fileSize": 1234,
    }


@respx.mock
async def test_notify_batch_uploaded_accepted() -> None:
    respx.post(NOTIFY_PATH).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ACCEPTED",
                "processingId": "P1",
                "resultFilePath": "s3://r",
                "message": None,
            },
        )
    )
    resp = await _import_client().notify_batch_uploaded(payload=_payload())
    assert resp["status"] == "ACCEPTED"
    assert resp["processingId"] == "P1"


@respx.mock
async def test_notify_duplicate_batch_returns_body() -> None:
    respx.post(NOTIFY_PATH).mock(
        return_value=httpx.Response(200, json={"status": "DUPLICATE_BATCH", "processingId": "P1"})
    )
    resp = await _import_client().notify_batch_uploaded(payload=_payload())
    assert resp["status"] == "DUPLICATE_BATCH"


@respx.mock
async def test_notify_400_checksum_mismatch_returns_body_not_raise() -> None:
    """400 校验类错误返回 body（status 载明），不抛——调用方据此修复重传。"""
    respx.post(NOTIFY_PATH).mock(
        return_value=httpx.Response(400, json={"status": "CHECKSUM_MISMATCH", "message": "bad"})
    )
    resp = await _import_client().notify_batch_uploaded(payload=_payload())
    assert resp["status"] == "CHECKSUM_MISMATCH"


@respx.mock
async def test_notify_401_apikey_rejected_raises() -> None:
    respx.post(NOTIFY_PATH).mock(return_value=httpx.Response(401, json={"status": "UNAUTHORIZED"}))
    with pytest.raises(WedapError) as exc:
        await _import_client().notify_batch_uploaded(payload=_payload())
    assert exc.value.code == "401"


@respx.mock
async def test_notify_5xx_raises() -> None:
    respx.post(NOTIFY_PATH).mock(return_value=httpx.Response(503, text="unavailable"))
    with pytest.raises(httpx.HTTPStatusError):
        await _import_client().notify_batch_uploaded(payload=_payload())


@respx.mock
async def test_notify_sends_apikey_header() -> None:
    route = respx.post(NOTIFY_PATH).mock(
        return_value=httpx.Response(200, json={"status": "ACCEPTED"})
    )
    await _import_client().notify_batch_uploaded(payload=_payload())
    assert route.calls.last.request.headers["apikey"] == "KEY123"


# --- 网关拒绝锚点(success=false) + 7 值 status 全覆盖(ADR-0001 §外部错误契约) ---


@respx.mock
async def test_notify_gateway_rejection_success_false_400_raises() -> None:
    respx.post(NOTIFY_PATH).mock(
        return_value=httpx.Response(
            400,
            json={"success": False, "error": {"code": "MISSING_REQUEST_ID", "message": "x"}},
        )
    )
    with pytest.raises(WedapGatewayRejected) as exc:
        await _import_client().notify_batch_uploaded(payload=_payload())
    assert exc.value.http_status == 400
    assert exc.value.code == "MISSING_REQUEST_ID"


@respx.mock
async def test_notify_gateway_rejection_success_false_401_is_gateway_not_plain_401() -> None:
    # APISIX key-auth 拒绝: HTTP 401 但带 success:false → 归网关拒绝(WedapGatewayRejected),
    # 非上游应用层 401(WedapError)。error 缺 message → 覆盖 message 兜底分支。
    respx.post(NOTIFY_PATH).mock(
        return_value=httpx.Response(401, json={"success": False, "error": {"code": "UNAUTHORIZED"}})
    )
    with pytest.raises(WedapGatewayRejected) as exc:
        await _import_client().notify_batch_uploaded(payload=_payload())
    assert exc.value.http_status == 401


@respx.mock
async def test_notify_gateway_rejection_429_no_error_dict_falls_back() -> None:
    # success:false 但无 error dict → code/message 走兜底(GATEWAY_REJECTED)。
    respx.post(NOTIFY_PATH).mock(return_value=httpx.Response(429, json={"success": False}))
    with pytest.raises(WedapGatewayRejected) as exc:
        await _import_client().notify_batch_uploaded(payload=_payload())
    assert exc.value.http_status == 429
    assert exc.value.code == "GATEWAY_REJECTED"


@respx.mock
async def test_notify_unknown_status_raises_not_silent_delivered() -> None:
    # codex P1：未识别 status 若只 warn+return 会被 dispatch 记 DELIVERED（误当受理成功）→ 改抛。
    respx.post(NOTIFY_PATH).mock(return_value=httpx.Response(200, json={"status": "WEIRD_UNSEEN"}))
    with pytest.raises(WedapError) as exc:
        await _import_client().notify_batch_uploaded(payload=_payload())
    assert exc.value.code == "UNKNOWN_STATUS"
    assert "WEIRD_UNSEEN" in str(exc.value)


@respx.mock
async def test_notify_missing_status_raises() -> None:
    # 200 但无 status 字段（且无 success）→ 无法确认受理 → 抛，不静默 DELIVERED。
    respx.post(NOTIFY_PATH).mock(return_value=httpx.Response(200, json={}))
    with pytest.raises(WedapError) as exc:
        await _import_client().notify_batch_uploaded(payload=_payload())
    assert exc.value.code == "UNKNOWN_STATUS"


@respx.mock
@pytest.mark.parametrize("bad_body", [[1, 2], None, "plain-string", 42])
async def test_notify_non_dict_body_raises(bad_body) -> None:
    # codex P2：list / null / 标量体 → body={} → status 缺失 → 抛（不被误当受理成功）。
    respx.post(NOTIFY_PATH).mock(return_value=httpx.Response(200, json=bad_body))
    with pytest.raises(WedapError) as exc:
        await _import_client().notify_batch_uploaded(payload=_payload())
    assert exc.value.code == "UNKNOWN_STATUS"


@respx.mock
@pytest.mark.parametrize("status", sorted(KNOWN_BATCH_STATUS))
async def test_notify_all_seven_known_statuses_return_body(status: str) -> None:
    respx.post(NOTIFY_PATH).mock(return_value=httpx.Response(200, json={"status": status}))
    resp = await _import_client().notify_batch_uploaded(payload=_payload())
    assert resp["status"] == status


def test_known_batch_status_is_the_seven_web2core_values() -> None:
    assert KNOWN_BATCH_STATUS == {
        "ACCEPTED",
        "DUPLICATE_BATCH",
        "DUPLICATE_BATCH_CONFLICT",
        "FILE_NOT_FOUND",
        "CHECKSUM_MISMATCH",
        "INVALID_PARAM",
        "REPLACES_BATCH_NOT_FOUND",
    }
