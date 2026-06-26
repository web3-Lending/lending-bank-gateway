"""gateway→recon wedap 投递回执客户端（§4.3「A+异步回执」）。

gateway 投递终态后回写 recon 权威态。POST recon /api/v1/wedap-export/delivery-callback，
S2S 头 X-Caller-Service=lending-bank-gateway + X-Tenant-Id + X-Request-Id=
wedap-delivery-result-{importBatchNo}（recon 回执幂等键）。
"""

from __future__ import annotations

import httpx

_CALLBACK_PATH = "/api/v1/wedap-export/delivery-callback"
_CALLER = "lending-bank-gateway"


def build_callback_request_id(import_batch_no: str) -> str:
    """gateway→recon 回执幂等键。"""
    return f"wedap-delivery-result-{import_batch_no}"


class ReconCallbackClient:
    """recon 投递回执客户端（短生命周期 AsyncClient）。"""

    def __init__(self, *, base_url: str, timeout: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def post_result(
        self,
        *,
        tenant_id: str,
        import_batch_no: str,
        status: str,
        error: str | None = None,
    ) -> None:
        """回写投递终态；非 2xx 抛 httpx.HTTPStatusError（调用方决定是否吞）。"""
        headers = {
            "X-Caller-Service": _CALLER,
            "X-Tenant-Id": tenant_id,
            "X-Request-Id": build_callback_request_id(import_batch_no),
            "Content-Type": "application/json",
        }
        body = {"import_batch_no": import_batch_no, "status": status, "error": error}
        async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout) as client:
            resp = await client.post(_CALLBACK_PATH, json=body, headers=headers)
            resp.raise_for_status()
