"""wedap 通用冲正 client 方法：reverse() → POST /api/v1/transactions/reversal。"""

from unittest.mock import AsyncMock

import pytest

from app.clients.wedap import WedapClient


@pytest.mark.asyncio
async def test_reverse_posts_to_general_reversal_path() -> None:
    client = WedapClient(base_url="http://wedap.test", timeout_seconds=1.0)
    client._post = AsyncMock(return_value={"txnStatus": "REVERSED", "bizSeqNo": "RVSL-1"})  # type: ignore[method-assign]
    payload = {
        "bizSeqNo": "RVSL-1",
        "transType": "BANK_FUND_COLLECT_LOAN",
        "oriBizSeqNo": "CLT-1",
        "oriReqDate": "20260722",
        "oriTxnAmount": "5000.0000",
        "currencyCode": "USD",
    }
    data = await client.reverse(tenant_id="WBTHK01", request_id="req-1", payload=payload)
    assert data["txnStatus"] == "REVERSED"
    client._post.assert_awaited_once()
    args, kwargs = client._post.call_args
    assert args[0] == "/api/v1/transactions/reversal"
    assert kwargs["tenant_id"] == "WBTHK01"
    assert kwargs["payload"] == payload
