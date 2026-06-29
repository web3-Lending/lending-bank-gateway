import httpx
import pytest
import respx

from app.clients.recon_callback import ReconCallbackClient, build_callback_request_id


def test_build_callback_request_id():
    assert (
        build_callback_request_id("BATCH-LEN-20260624-001")
        == "wedap-delivery-result-BATCH-LEN-20260624-001"
    )


@pytest.mark.asyncio
@respx.mock
async def test_post_result_sends_s2s_headers_and_body():
    route = respx.post("http://recon:8040/api/v1/wedap-export/delivery-callback").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    await ReconCallbackClient(base_url="http://recon:8040/").post_result(
        tenant_id="WBTHK01", import_batch_no="BATCH-LEN-20260624-001", status="DELIVERED"
    )
    req = route.calls.last.request
    assert req.headers["X-Caller-Service"] == "lending-bank-gateway"
    assert req.headers["X-Tenant-Id"] == "WBTHK01"
    assert req.headers["X-Request-Id"] == "wedap-delivery-result-BATCH-LEN-20260624-001"
    import json

    sent = json.loads(req.content)
    assert sent == {
        "import_batch_no": "BATCH-LEN-20260624-001",
        "status": "DELIVERED",
        "error": None,
    }


@pytest.mark.asyncio
@respx.mock
async def test_post_result_raises_on_4xx():
    respx.post("http://recon:8040/api/v1/wedap-export/delivery-callback").mock(
        return_value=httpx.Response(403)
    )
    with pytest.raises(httpx.HTTPStatusError):
        await ReconCallbackClient(base_url="http://recon:8040").post_result(
            tenant_id="WBTHK01", import_batch_no="B1", status="FAILED", error="boom"
        )
