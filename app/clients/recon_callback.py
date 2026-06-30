"""gateway→recon wedap 投递回执客户端（§4.3「A+异步回执」）。

gateway 投递终态后回写 recon 权威态。POST recon /api/v1/wedap-export/delivery-callback。
鉴权（FU-WEDAP-CALLBACK-HMAC）：复用 recon 写接口 4 头 HMAC——
X-Caller-Service + X-Timestamp + X-Nonce + X-Body-SHA256 + X-Signature
= HMAC-SHA256(secret, "POST\\npath\\ntimestamp\\nnonce\\nbody_sha256")。
hmac_secret 为空时发占位签名（recon local insecure 跳验算，但 4 头仍须存在）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from typing import Any

import httpx

_CALLBACK_PATH = "/api/v1/wedap-export/delivery-callback"
_LINE_RESULTS_PATH = "/api/v1/wedap-export/line-results"
_CALLER = "lending-bank-gateway"
_UNSIGNED = "0" * 64  # hmac_secret 未配时占位（recon insecure 不校验签名）


def build_callback_request_id(import_batch_no: str) -> str:
    """gateway→recon 回执幂等键。"""
    return f"wedap-delivery-result-{import_batch_no}"


def _sign(secret: str, path: str, *, timestamp: str, nonce: str, body_sha256: str) -> str:
    """HMAC-SHA256(secret, POST\\npath\\ntimestamp\\nnonce\\nbody_sha256)（按真实请求 path 签）。"""
    msg = f"POST\n{path}\n{timestamp}\n{nonce}\n{body_sha256}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


class ReconCallbackClient:
    """recon 投递回执客户端（短生命周期 AsyncClient）。"""

    def __init__(
        self, *, base_url: str, hmac_secret: str | None = None, timeout: float = 10.0
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._hmac_secret = hmac_secret
        self._timeout = timeout

    async def post_result(
        self,
        *,
        tenant_id: str,
        import_batch_no: str,
        status: str,
        error: str | None = None,
    ) -> None:
        """回写投递终态（HMAC 签名）；非 2xx 抛 httpx.HTTPStatusError（调用方决定是否吞）。"""
        body = {"import_batch_no": import_batch_no, "status": status, "error": error}
        # 精确字节（与 X-Body-SHA256 一致）；recon require_hmac_headers 按原始 body 验 hash。
        body_bytes = json.dumps(body, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        nonce = uuid.uuid4().hex
        body_sha256 = hashlib.sha256(body_bytes).hexdigest()
        signature = (
            _sign(
                self._hmac_secret,
                _CALLBACK_PATH,
                timestamp=timestamp,
                nonce=nonce,
                body_sha256=body_sha256,
            )
            if self._hmac_secret
            else _UNSIGNED
        )
        headers = {
            "X-Caller-Service": _CALLER,
            "X-Tenant-Id": tenant_id,
            "X-Request-Id": build_callback_request_id(import_batch_no),
            "X-Timestamp": timestamp,
            "X-Nonce": nonce,
            "X-Body-SHA256": body_sha256,
            "X-Signature": signature,
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout) as client:
            resp = await client.post(_CALLBACK_PATH, content=body_bytes, headers=headers)
            resp.raise_for_status()

    async def post_line_results(
        self,
        *,
        tenant_id: str,
        import_batch_no: str,
        data_type: str,
        line_results: list[dict[str, Any]],
    ) -> None:
        """回传逐条 lineResult 给 recon（签 line-results path）；非 2xx 抛 HTTPStatusError。"""
        body = {
            "import_batch_no": import_batch_no,
            "data_type": data_type,
            "line_results": line_results,
        }
        body_bytes = json.dumps(body, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        nonce = uuid.uuid4().hex
        body_sha256 = hashlib.sha256(body_bytes).hexdigest()
        signature = (
            _sign(
                self._hmac_secret,
                _LINE_RESULTS_PATH,
                timestamp=timestamp,
                nonce=nonce,
                body_sha256=body_sha256,
            )
            if self._hmac_secret
            else _UNSIGNED
        )
        headers = {
            "X-Caller-Service": _CALLER,
            "X-Tenant-Id": tenant_id,
            "X-Request-Id": f"wedap-line-results-{import_batch_no}",
            "X-Timestamp": timestamp,
            "X-Nonce": nonce,
            "X-Body-SHA256": body_sha256,
            "X-Signature": signature,
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout) as client:
            resp = await client.post(_LINE_RESULTS_PATH, content=body_bytes, headers=headers)
            resp.raise_for_status()
