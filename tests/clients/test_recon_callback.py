import hashlib
import hmac
import json

import httpx
import pytest
import respx

from app.clients.recon_callback import (
    ReconCallbackClient,
    build_callback_request_id,
)

_PATH = "/api/v1/wedap-export/delivery-callback"


def test_build_callback_request_id():
    assert (
        build_callback_request_id("BATCH-LEN-20260624-001")
        == "wedap-delivery-result-BATCH-LEN-20260624-001"
    )


@pytest.mark.asyncio
@respx.mock
async def test_post_result_signs_hmac_and_sends_body():
    route = respx.post(f"http://recon:8040{_PATH}").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    await ReconCallbackClient(
        base_url="http://recon:8040/",
        hmac_secret="shared-secret",  # noqa: S106
    ).post_result(tenant_id="WBTHK01", import_batch_no="BATCH-LEN-20260624-001", status="DELIVERED")
    req = route.calls.last.request
    assert req.headers["X-Caller-Service"] == "lending-bank-gateway"
    assert req.headers["X-Tenant-Id"] == "WBTHK01"
    assert req.headers["X-Request-Id"] == "wedap-delivery-result-BATCH-LEN-20260624-001"

    # body 以原始字节发送（与 X-Body-SHA256 一致）
    sent = json.loads(req.content)
    assert sent == {
        "import_batch_no": "BATCH-LEN-20260624-001",
        "status": "DELIVERED",
        "error": None,
    }
    assert req.headers["X-Body-SHA256"] == hashlib.sha256(req.content).hexdigest()

    # 签名 = HMAC-SHA256(secret, POST\npath\nts\nnonce\nbody_sha256)，与 recon 验签一致
    ts, nonce, bh = req.headers["X-Timestamp"], req.headers["X-Nonce"], req.headers["X-Body-SHA256"]
    expected = hmac.new(
        b"shared-secret", f"POST\n{_PATH}\n{ts}\n{nonce}\n{bh}".encode(), hashlib.sha256
    ).hexdigest()
    assert req.headers["X-Signature"] == expected


@pytest.mark.asyncio
@respx.mock
async def test_post_result_unsigned_placeholder_when_no_secret():
    route = respx.post(f"http://recon:8040{_PATH}").mock(return_value=httpx.Response(200, json={}))
    await ReconCallbackClient(base_url="http://recon:8040").post_result(
        tenant_id="WBTHK01", import_batch_no="B1", status="DELIVERED"
    )
    # 无 secret → 占位签名（recon insecure 跳验算，但 4 头仍存在）
    req = route.calls.last.request
    assert req.headers["X-Signature"] == "0" * 64
    assert req.headers["X-Timestamp"] and req.headers["X-Nonce"]


@pytest.mark.asyncio
@respx.mock
async def test_post_result_raises_on_4xx():
    respx.post(f"http://recon:8040{_PATH}").mock(return_value=httpx.Response(403))
    with pytest.raises(httpx.HTTPStatusError):
        await ReconCallbackClient(base_url="http://recon:8040").post_result(
            tenant_id="WBTHK01", import_batch_no="B1", status="FAILED", error="boom"
        )


_LR_PATH = "/api/v1/wedap-export/line-results"


@pytest.mark.asyncio
@respx.mock
async def test_post_line_results_signs_line_results_path():
    route = respx.post(f"http://recon:8040{_LR_PATH}").mock(
        return_value=httpx.Response(200, json={"inserted": 1})
    )
    await ReconCallbackClient(
        base_url="http://recon:8040/",
        hmac_secret="shared-secret",  # noqa: S106
    ).post_line_results(
        tenant_id="WBTHK01",
        import_batch_no="BATCH-LEN-20260630-001",
        data_type="loan-detail",
        line_results=[
            {
                "line_no": 4,
                "line_status": "LINE_PARSE_ERROR",
                "error_code": "REQUIRED_FIELD_MISSING",
            },
        ],
    )
    req = route.calls.last.request
    assert req.headers["X-Caller-Service"] == "lending-bank-gateway"
    assert req.headers["X-Request-Id"] == "wedap-line-results-BATCH-LEN-20260630-001"
    sent = json.loads(req.content)
    assert sent["import_batch_no"] == "BATCH-LEN-20260630-001"
    assert sent["data_type"] == "loan-detail"
    assert len(sent["line_results"]) == 1
    # 签名按 line-results path（非 callback path）——换 endpoint 必须重签
    ts, nonce, bh = req.headers["X-Timestamp"], req.headers["X-Nonce"], req.headers["X-Body-SHA256"]
    expected = hmac.new(
        b"shared-secret", f"POST\n{_LR_PATH}\n{ts}\n{nonce}\n{bh}".encode(), hashlib.sha256
    ).hexdigest()
    assert req.headers["X-Signature"] == expected
