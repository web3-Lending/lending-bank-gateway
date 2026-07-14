import json
import pathlib

import httpx
import pytest
import respx

from app.clients.wedap import (
    KNOWN_BATCH_STATUS,
    WedapClient,
    WedapError,
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


# --- 网关错误(gw-internal CommonResponse{code,message}) + 7 值 status 全覆盖(外部错误契约) ---


@respx.mock
async def test_notify_gateway_common_response_error_raises_with_its_code() -> None:
    # gw-internal GlobalErrorWebExceptionHandler 统一错误形态：CommonResponse{code,message}
    # （无 status 字段）→ 按其 code/message 抛 WedapError，不落 UNKNOWN_STATUS。
    respx.post(NOTIFY_PATH).mock(
        return_value=httpx.Response(
            404, json={"code": "GW_404", "message": "no route matched", "data": None}
        )
    )
    with pytest.raises(WedapError) as exc:
        await _import_client().notify_batch_uploaded(payload=_payload())
    assert exc.value.code == "GW_404"
    assert "no route matched" in str(exc.value)


@respx.mock
async def test_notify_gateway_401_common_response_is_plain_401() -> None:
    # 401 无论体形态（网关 CommonResponse / web2-core 应用层）统一归 apikey 未对齐。
    respx.post(NOTIFY_PATH).mock(
        return_value=httpx.Response(401, json={"code": "UNAUTHORIZED", "message": "denied"})
    )
    with pytest.raises(WedapError) as exc:
        await _import_client().notify_batch_uploaded(payload=_payload())
    assert exc.value.code == "401"


@respx.mock
async def test_notify_gateway_429_common_response_raises_with_code() -> None:
    # 非 401/5xx 的网关错误（如限流 429）→ 按 CommonResponse code 抛，可读文案不丢。
    respx.post(NOTIFY_PATH).mock(
        return_value=httpx.Response(429, json={"code": "TOO_MANY", "message": "rate limited"})
    )
    with pytest.raises(WedapError) as exc:
        await _import_client().notify_batch_uploaded(payload=_payload())
    assert exc.value.code == "TOO_MANY"
    assert "rate limited" in str(exc.value)


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
async def test_notify_non_contract_error_body_falls_to_unknown_status() -> None:
    # 既无 status 也无 code 的错误体（非契约形态）→ 兜底 UNKNOWN_STATUS，不静默 DELIVERED。
    respx.post(NOTIFY_PATH).mock(
        return_value=httpx.Response(400, json={"success": False, "error": "boom"})
    )
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


# --- 请求头（gw-internal：无网关签名，apikey 应用层鉴权 + X-Request-Id 追踪头恒发）---


@respx.mock
async def test_notify_headers_no_signature_and_request_id_always_sent() -> None:
    route = respx.post(NOTIFY_PATH).mock(
        return_value=httpx.Response(200, json={"status": "ACCEPTED"})
    )
    await _import_client().notify_batch_uploaded(payload=_payload())
    req = route.calls.last.request
    assert req.headers["X-Request-Id"] == "wedap-import-BATCH-LEN-20260624-001"
    # gw-internal Phase 1 无网关鉴权：不再有 APISIX HMAC 签名头
    assert "X-Signature" not in req.headers
    assert "X-Nonce" not in req.headers
    assert "X-Timestamp" not in req.headers


def test_header_safe_sanitizes_unsafe_chars() -> None:
    from app.clients.wedap import _header_safe

    assert _header_safe("BATCH-LEN-20260624-001") == "BATCH-LEN-20260624-001"  # 正常值原样
    assert _header_safe("a\r\nb\tc 日本") == "a--b-c---"  # CR/LF/tab/空格/非ASCII → -（: 保留）
    assert len(_header_safe("x" * 200)) == 80  # 截断


@respx.mock
async def test_notify_sanitizes_batch_no_in_request_id_header() -> None:
    # codex P1：importBatchNo 含 CR/LF/非ASCII 时,X-Request-Id 不得注入非法 header。
    route = respx.post(NOTIFY_PATH).mock(
        return_value=httpx.Response(200, json={"status": "ACCEPTED"})
    )
    payload = {**_payload(), "importBatchNo": "EVIL\r\nX-Injected: 1\t日本"}
    await _import_client().notify_batch_uploaded(payload=payload)
    req = route.calls.last.request
    rid = req.headers["X-Request-Id"]
    assert "\r" not in rid and "\n" not in rid and "\t" not in rid
    assert rid.startswith("wedap-import-EVIL--X-Injected:-1")  # : 保留,CR/LF/tab/非ASCII → -
    assert "X-Injected" not in req.headers  # 注入的头名没被当成真头


# ---------------------------------------------------------------------------
# request_presign (flow-import P4 预签名)
# ---------------------------------------------------------------------------

PRESIGN_PATH = f"{BASE}/bank/api/v1/import/presign"


def _presign_ok_body(operation: str, method: str) -> dict:
    return {
        "status": "OK",
        "operation": operation,
        "method": method,
        "url": f"https://s3.example/presigned-{operation.lower()}?sig=abc",
        "objectKey": f"lending/{operation.lower()}/x",
        "expiresInSeconds": 900,
    }


async def _do_presign(client: WedapClient, operation: str = "UPLOAD") -> str:
    return await client.request_presign(
        operation=operation,
        data_type="interest-accrual",
        channel_id="LEN",
        import_date="20260624",
        import_batch_no="BATCH-LEN-20260624-001",
    )


@respx.mock
async def test_presign_upload_returns_url() -> None:
    route = respx.post(PRESIGN_PATH).mock(
        return_value=httpx.Response(200, json=_presign_ok_body("UPLOAD", "PUT"))
    )
    url = await _do_presign(_import_client(), "UPLOAD")
    assert url == "https://s3.example/presigned-upload?sig=abc"
    # POST 到 presign 路径 + 请求体带契约字段
    req = route.calls.last.request
    body = json.loads(req.content.decode())
    assert body["operation"] == "UPLOAD"
    assert body["dataType"] == "interest-accrual"
    assert body["channelId"] == "LEN"
    assert body["importBatchNo"] == "BATCH-LEN-20260624-001"
    assert body["importDate"] == "20260624"


@respx.mock
async def test_presign_result_returns_url() -> None:
    respx.post(PRESIGN_PATH).mock(
        return_value=httpx.Response(200, json=_presign_ok_body("RESULT", "GET"))
    )
    url = await _do_presign(_import_client(), "RESULT")
    assert url == "https://s3.example/presigned-result?sig=abc"


@respx.mock
async def test_presign_sends_apikey_header() -> None:
    route = respx.post(PRESIGN_PATH).mock(
        return_value=httpx.Response(200, json=_presign_ok_body("UPLOAD", "PUT"))
    )
    await _do_presign(_import_client())
    assert route.calls.last.request.headers["apikey"] == "KEY123"


@respx.mock
async def test_presign_headers_no_signature_and_request_id_always_sent() -> None:
    route = respx.post(PRESIGN_PATH).mock(
        return_value=httpx.Response(200, json=_presign_ok_body("UPLOAD", "PUT"))
    )
    await _do_presign(_import_client(), "UPLOAD")
    req = route.calls.last.request
    assert req.headers["X-Request-Id"] == "wedap-presign-UPLOAD-BATCH-LEN-20260624-001"
    # gw-internal Phase 1 无网关鉴权：不再有 APISIX HMAC 签名头
    assert "X-Signature" not in req.headers
    assert "X-Nonce" not in req.headers
    assert "X-Timestamp" not in req.headers


@respx.mock
async def test_presign_gateway_common_response_error_raises_with_its_code() -> None:
    # gw-internal 网关错误统一形态 CommonResponse{code,message}（无 status）→ 按其 code 抛。
    respx.post(PRESIGN_PATH).mock(
        return_value=httpx.Response(
            404, json={"code": "GW_404", "message": "no route matched", "data": None}
        )
    )
    with pytest.raises(WedapError) as exc:
        await _do_presign(_import_client())
    assert exc.value.code == "GW_404"
    assert "no route matched" in str(exc.value)


@respx.mock
async def test_presign_gateway_429_common_response_raises_with_code() -> None:
    respx.post(PRESIGN_PATH).mock(
        return_value=httpx.Response(429, json={"code": "TOO_MANY", "message": "rate limited"})
    )
    with pytest.raises(WedapError) as exc:
        await _do_presign(_import_client())
    assert exc.value.code == "TOO_MANY"


@respx.mock
async def test_presign_plain_401_raises_wedap_error() -> None:
    respx.post(PRESIGN_PATH).mock(return_value=httpx.Response(401, json={"status": "UNAUTHORIZED"}))
    with pytest.raises(WedapError) as exc:
        await _do_presign(_import_client())
    assert exc.value.code == "401"


@respx.mock
async def test_presign_5xx_raises() -> None:
    respx.post(PRESIGN_PATH).mock(return_value=httpx.Response(503, text="unavailable"))
    with pytest.raises(httpx.HTTPStatusError):
        await _do_presign(_import_client())


@respx.mock
async def test_presign_status_not_ok_raises() -> None:
    respx.post(PRESIGN_PATH).mock(
        return_value=httpx.Response(200, json={"status": "ERROR", "message": "bad op"})
    )
    with pytest.raises(WedapError) as exc:
        await _do_presign(_import_client())
    assert exc.value.code == "PRESIGN_FAILED"


@respx.mock
@pytest.mark.parametrize("bad_url", [None, "", 42, []])
async def test_presign_missing_or_bad_url_raises(bad_url) -> None:
    respx.post(PRESIGN_PATH).mock(
        return_value=httpx.Response(200, json={"status": "OK", "url": bad_url})
    )
    with pytest.raises(WedapError) as exc:
        await _do_presign(_import_client())
    assert exc.value.code == "PRESIGN_FAILED"


# ---------------------------------------------------------------------------
# gw-internal 对接形态（银行南向直连式无签名 + flow-import 独立 base）
# ---------------------------------------------------------------------------

IMPORT_BASE = "http://gw-internal:8000/external/web2-core"


def _split_base_client() -> WedapClient:
    """银行南向与 flow-import 各自 base（gw-internal：/lending-gw vs /external/web2-core）。"""
    return WedapClient(
        base_url=BASE,
        timeout_seconds=1.0,
        import_api_key="KEY123",
        import_base_url=IMPORT_BASE,
    )


@respx.mock
async def test_bank_direct_mode_no_prefix_no_sign() -> None:
    # 银行南向唯一形态：base_url 直拼原始路径（gw-internal 前缀由 base_url 承载），
    # 无 apikey / 无 HMAC 签名头。
    route = respx.post(f"{BASE}/api/v1/loans/p2p-disbursements").mock(
        return_value=httpx.Response(
            200, json=json.loads((FIX / "disbursement_accepted.json").read_text())
        )
    )
    await _client().submit_disbursement(tenant_id="OCBC", request_id="r", payload={"bizSeqNo": "X"})
    req = route.calls.last.request
    assert "apikey" not in req.headers
    assert "X-Signature" not in req.headers
    assert "X-Nonce" not in req.headers
    assert req.headers["X-Tenant-Id"] == "OCBC"


@respx.mock
async def test_bank_base_url_carries_gw_internal_prefix() -> None:
    # 经 gw-internal 时 base_url 自带 /lending-gw 前缀，client 不做任何路径改写。
    gw_base = "http://gw-internal:8000/lending-gw"
    route = respx.get(f"{gw_base}/api/v1/loans/p2p-disbursements/DSB-9/status").mock(
        return_value=httpx.Response(200, json={"code": "200", "data": {"txnStatus": "SUCCESS"}})
    )
    c = WedapClient(base_url=gw_base, timeout_seconds=1.0)
    resp = await c.query_funds_status(
        tenant_id="WBTHK01", request_id="r", biz_seq_no="DSB-9", biz_type="DISB"
    )
    assert resp["txnStatus"] == "SUCCESS"
    assert route.called


@respx.mock
async def test_import_base_url_routes_notify_and_presign_separately() -> None:
    # flow-import 走独立 base：notify/presign 打到 import_base_url，银行南向仍打 base_url。
    notify_route = respx.post(f"{IMPORT_BASE}/bank/api/v1/import/batch-uploaded").mock(
        return_value=httpx.Response(200, json={"status": "ACCEPTED"})
    )
    presign_route = respx.post(f"{IMPORT_BASE}/bank/api/v1/import/presign").mock(
        return_value=httpx.Response(200, json=_presign_ok_body("UPLOAD", "PUT"))
    )
    bank_route = respx.post(f"{BASE}/api/v1/loans/p2p-repayments").mock(
        return_value=httpx.Response(200, json={"code": "200", "data": {"txnStatus": "PROCESSING"}})
    )
    c = _split_base_client()
    await c.notify_batch_uploaded(payload=_payload())
    await _do_presign(c)
    await c.submit_repayment(tenant_id="WBTHK01", request_id="r", payload={"bizSeqNo": "RP-1"})
    assert notify_route.called
    assert presign_route.called
    assert bank_route.called


@respx.mock
async def test_import_base_url_empty_falls_back_to_base_url() -> None:
    # import_base_url 未配（local/test）→ 回落 base_url（既有 NOTIFY_PATH 即此形态）。
    route = respx.post(NOTIFY_PATH).mock(
        return_value=httpx.Response(200, json={"status": "ACCEPTED"})
    )
    await _import_client().notify_batch_uploaded(payload=_payload())
    assert route.called


# ---------------------------------------------------------------------------
# 错误文案 msg|message 双字段兼容（baffle=msg，真 wedap AdapterResponse=message）
# ---------------------------------------------------------------------------


@respx.mock
async def test_unwrap_error_text_from_message_field_adapter_style() -> None:
    # 真 wedap（adapter AdapterResponse）错误体用 message；文案不得丢成 "None"。
    respx.post(f"{BASE}/api/v1/loans/p2p-disbursements").mock(
        return_value=httpx.Response(
            200, json={"code": "500", "message": "ROUTER_TIMEOUT", "data": None}
        )
    )
    with pytest.raises(WedapError) as exc:
        await _client().submit_disbursement(tenant_id="WBTHK01", request_id="r", payload={})
    assert exc.value.code == "500"
    assert "ROUTER_TIMEOUT" in str(exc.value)
    assert "None" not in str(exc.value)


@respx.mock
async def test_unwrap_error_text_from_msg_field_baffle_style() -> None:
    # baffle 错误体用 msg；兼容不回退。
    respx.post(f"{BASE}/api/v1/loans/p2p-disbursements").mock(
        return_value=httpx.Response(200, json={"code": "500", "msg": "SYSTEM_ERROR"})
    )
    with pytest.raises(WedapError) as exc:
        await _client().submit_disbursement(tenant_id="WBTHK01", request_id="r", payload={})
    assert "SYSTEM_ERROR" in str(exc.value)


# ---------------------------------------------------------------------------
# codex NEEDS-ATTENTION 修复回归（L1 非业务位 4xx 不可信 status；L2 _error_text 边界）
# ---------------------------------------------------------------------------


@respx.mock
async def test_notify_403_with_accepted_status_not_trusted() -> None:
    # L1：非 200/400 的 4xx（网关限流/拒绝）即使体里带合法 status 也不可信 → 抛，不记 DELIVERED。
    respx.post(NOTIFY_PATH).mock(return_value=httpx.Response(403, json={"status": "ACCEPTED"}))
    with pytest.raises(WedapError) as exc:
        await _import_client().notify_batch_uploaded(payload=_payload())
    assert exc.value.code == "HTTP_403"


@respx.mock
async def test_presign_429_with_ok_status_not_trusted() -> None:
    # L1：presign 只认 2xx；429 带 status=OK+url 也不返回 URL。
    respx.post(PRESIGN_PATH).mock(
        return_value=httpx.Response(429, json=_presign_ok_body("UPLOAD", "PUT"))
    )
    with pytest.raises(WedapError) as exc:
        await _do_presign(_import_client())
    assert exc.value.code == "HTTP_429"


@respx.mock
async def test_unwrap_error_text_blank_msg_falls_to_message() -> None:
    # L2：msg 为纯空白时不得遮住有效 message。
    respx.post(f"{BASE}/api/v1/loans/p2p-disbursements").mock(
        return_value=httpx.Response(200, json={"code": "500", "msg": " ", "message": "REAL_REASON"})
    )
    with pytest.raises(WedapError) as exc:
        await _client().submit_disbursement(tenant_id="WBTHK01", request_id="r", payload={})
    assert "REAL_REASON" in str(exc.value)


@respx.mock
async def test_unwrap_error_text_both_missing_stable_fallback() -> None:
    # L2：msg/message 均缺失 → 稳定兜底文案，不再输出字符串 "None"。
    respx.post(f"{BASE}/api/v1/loans/p2p-disbursements").mock(
        return_value=httpx.Response(200, json={"code": "500"})
    )
    with pytest.raises(WedapError) as exc:
        await _client().submit_disbursement(tenant_id="WBTHK01", request_id="r", payload={})
    assert "<no error message>" in str(exc.value)


@respx.mock
async def test_notify_400_common_response_no_status_raises_with_code() -> None:
    # 400 位的网关 CommonResponse（有 code 无 status）→ 按其 code 抛。
    respx.post(NOTIFY_PATH).mock(
        return_value=httpx.Response(400, json={"code": "GW_400", "message": "bad request"})
    )
    with pytest.raises(WedapError) as exc:
        await _import_client().notify_batch_uploaded(payload=_payload())
    assert exc.value.code == "GW_400"
    assert "bad request" in str(exc.value)


@respx.mock
async def test_presign_200_common_response_no_status_raises_with_code() -> None:
    # 2xx 位但体是 CommonResponse（有 code 无 status/url）→ 按其 code 抛，不落 PRESIGN_FAILED。
    respx.post(PRESIGN_PATH).mock(
        return_value=httpx.Response(200, json={"code": "GW_200_WRAPPED", "message": "unexpected"})
    )
    with pytest.raises(WedapError) as exc:
        await _do_presign(_import_client())
    assert exc.value.code == "GW_200_WRAPPED"


# ---------------------------------------------------------------------------
# 兜底评审修复回归：受理类 status 只信任 2xx 位；拒绝类 status 保留任意 4xx 业务位
# ---------------------------------------------------------------------------


@respx.mock
async def test_notify_400_with_accepted_status_not_trusted() -> None:
    # 400 位 + 受理体是矛盾响应（web2-core 契约 400 只承载拒绝类）→ 抛，不记 DELIVERED。
    respx.post(NOTIFY_PATH).mock(return_value=httpx.Response(400, json={"status": "ACCEPTED"}))
    with pytest.raises(WedapError) as exc:
        await _import_client().notify_batch_uploaded(payload=_payload())
    assert exc.value.code == "HTTP_400"


@respx.mock
async def test_notify_202_with_accepted_status_returns_body() -> None:
    # 受理类 status 在任意 2xx 位可信（201/202 等非 200 成功位不误伤为 FAILED 对账分叉）。
    respx.post(NOTIFY_PATH).mock(
        return_value=httpx.Response(202, json={"status": "ACCEPTED", "processingId": "P2"})
    )
    resp = await _import_client().notify_batch_uploaded(payload=_payload())
    assert resp["processingId"] == "P2"


@respx.mock
async def test_notify_422_with_rejection_status_returns_body() -> None:
    # 拒绝类 status 在非 400 的 4xx 位（REST 惯例 422/409）保留业务拒因，不降级泛化 HTTP 码。
    respx.post(NOTIFY_PATH).mock(
        return_value=httpx.Response(422, json={"status": "CHECKSUM_MISMATCH", "message": "bad"})
    )
    resp = await _import_client().notify_batch_uploaded(payload=_payload())
    assert resp["status"] == "CHECKSUM_MISMATCH"


@respx.mock
async def test_notify_unknown_status_with_code_uses_code_not_unknown() -> None:
    # 未识别 status 但带 code（中间层混合体）→ 用 code/message，不吞成 UNKNOWN_STATUS。
    respx.post(NOTIFY_PATH).mock(
        return_value=httpx.Response(
            200, json={"status": "REJECTED", "code": "GW_RATE_LIMIT", "message": "throttled"}
        )
    )
    with pytest.raises(WedapError) as exc:
        await _import_client().notify_batch_uploaded(payload=_payload())
    assert exc.value.code == "GW_RATE_LIMIT"
    assert "throttled" in str(exc.value)


@respx.mock
async def test_unwrap_error_text_non_string_payload_stringified() -> None:
    # 非字符串 truthy 错误载荷（数字码/结构化对象）str 化保留，不丢诊断信息。
    respx.post(f"{BASE}/api/v1/loans/p2p-disbursements").mock(
        return_value=httpx.Response(
            200, json={"code": "500", "message": {"detail": "checksum mismatch at line 3"}}
        )
    )
    with pytest.raises(WedapError) as exc:
        await _client().submit_disbursement(tenant_id="WBTHK01", request_id="r", payload={})
    assert "checksum mismatch at line 3" in str(exc.value)
