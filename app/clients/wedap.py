import base64
import hashlib
import hmac
import json
import re
import time
import uuid
from typing import Any

import httpx

# flow-import notify 的入站路径（APISIX 专用 route5，见 ADR-0001 P2）；HMAC 签名串用它。
_IMPORT_PATH = "/bank/api/v1/import/batch-uploaded"


def _hmac_sign(secret: str, *, method: str, path: str, timestamp: str, body: str) -> str:
    """按 wedap APISIX hmac-sign 契约签名：HMAC-SHA256(METHOD\\nPATH\\nTIMESTAMP\\nBODY) → base64。

    与 recon 回调的签名方案不同（那个签 X-Body-SHA256）；此处对齐 APISIX hmac-sign.lua：
    签名串 = ``METHOD + "\\n" + PATH + "\\n" + TIMESTAMP + "\\n" + BODY``（BODY 非空时才拼）。
    """
    sign_str = f"{method}\n{path}\n{timestamp}"
    if body:
        sign_str += f"\n{body}"
    digest = hmac.new(secret.encode(), sign_str.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def _header_safe(value: object, *, max_len: int = 80) -> str:
    """把业务号清洗成 header-safe 值（防 CR/LF/非 ASCII 造成非法 header / 注入）。

    非 ``[A-Za-z0-9._:-]`` 一律替换为 ``-``，并截断到 ``max_len``。正常业务号
    （如 ``BATCH-LEN-20260624-001``）保持原样，异常输入不再进 header 原文。
    """
    return re.sub(r"[^A-Za-z0-9._:-]", "-", str(value))[:max_len]


# web2-core BatchUploadedResponse.Status 的 7 值枚举（ADR-0001 §外部错误契约）。
# lending 须对这 7 值全覆盖分支；未识别 / 缺失 status 直接抛 WedapError（不静默返回，
# 否则会被 dispatch_delivery_once 当作正常返回记为 DELIVERED → 误当受理成功）。
KNOWN_BATCH_STATUS = frozenset(
    {
        "ACCEPTED",
        "DUPLICATE_BATCH",
        "DUPLICATE_BATCH_CONFLICT",
        "FILE_NOT_FOUND",
        "CHECKSUM_MISMATCH",
        "INVALID_PARAM",
        "REPLACES_BATCH_NOT_FOUND",
    }
)


class WedapError(Exception):
    def __init__(self, code: str, msg: str) -> None:
        super().__init__(f"wedap {code}: {msg}")
        self.code = code


class WedapGatewayRejected(WedapError):
    """网关（APISIX）拒绝：响应带 ``success=false``。区别于上游应用层 401 / 业务响应。

    ADR-0001 §外部错误契约：lending 唯一可靠的"网关拒绝"锚点 = ``success`` 字段为 ``false``。
    ``http_status`` 承载网关拒绝的 HTTP 码，供上层按错误契约表分类重试（401/403/400 不重试；
    429/502/503 退避重试）——分类目前尚未接入 dispatch_delivery_once（当前统一退避重试到上限），
    见 FU（P1b 重试策略）。注意本类是 WedapError 子类，调用方若按错误契约分流，须先 ``except
    WedapGatewayRejected`` 再 ``except WedapError``，否则网关拒绝会被后者吞掉。
    """

    def __init__(self, http_status: int, code: str | None, message: str | None) -> None:
        super().__init__(
            code or "GATEWAY_REJECTED",
            message or f"gateway rejected (HTTP {http_status})",
        )
        self.http_status = http_status


class WedapClient:
    """南向 client：所有外呼带 timeout（规范07 §1），写操作不自动重试。

    幂等靠上层 RESULT_UNKNOWN 收敛。
    """

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        import_api_key: str | None = None,
        import_signing_secret: str | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._import_api_key = import_api_key
        # HMAC 签名密钥（对齐 APISIX LENDING consumer 的 signing_key）。为空 = 不签名
        # （P0/P1 直连 baffle 无 APISIX 时；切流到 APISIX 后必须配，否则被 hmac-sign 拦 400）。
        self._import_signing_secret = import_signing_secret

    def _headers(self, tenant_id: str, request_id: str) -> dict[str, str]:
        return {
            "X-Tenant-Id": tenant_id,
            "X-Request-Id": request_id,
            "Content-Type": "application/json",
        }

    async def _post(
        self,
        path: str,
        *,
        tenant_id: str,
        request_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.post(
                f"{self._base}{path}",
                json=payload,
                headers=self._headers(tenant_id, request_id),
            )
        return self._unwrap(r)

    async def _get(
        self,
        path: str,
        *,
        tenant_id: str,
        request_id: str,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.get(
                f"{self._base}{path}",
                params=params,
                headers=self._headers(tenant_id, request_id),
            )
        return self._unwrap(r)

    @staticmethod
    def _unwrap(r: httpx.Response) -> dict[str, Any]:
        r.raise_for_status()
        body: dict[str, Any] = r.json()
        if str(body.get("code")) != "200":
            raise WedapError(str(body.get("code")), str(body.get("msg")))
        return dict(body.get("data") or {})

    async def submit_disbursement(
        self,
        *,
        tenant_id: str,
        request_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._post(
            "/api/v1/loans/p2p-disbursements",
            tenant_id=tenant_id,
            request_id=request_id,
            payload=payload,
        )

    async def submit_repayment(
        self,
        *,
        tenant_id: str,
        request_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._post(
            "/api/v1/loans/p2p-repayments",
            tenant_id=tenant_id,
            request_id=request_id,
            payload=payload,
        )

    async def collect_from_users(
        self,
        *,
        tenant_id: str,
        request_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """归集资金。南向路径以 wedap-adapter 实测为准（2026-06-12 verify）。"""
        return await self._post(
            "/api/v1/bank-funds/user-collections",
            tenant_id=tenant_id,
            request_id=request_id,
            payload=payload,
        )

    async def distribute_to_users(
        self,
        *,
        tenant_id: str,
        request_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """分发资金。南向路径以 wedap-adapter 实测为准（2026-06-12 verify）。"""
        return await self._post(
            "/api/v1/bank-funds/user-distributions",
            tenant_id=tenant_id,
            request_id=request_id,
            payload=payload,
        )

    # biz_type → wedap 状态查询路径映射（wedap 无统一 /bank-funds/status 接口）
    _STATUS_PATH_TMPL: dict[str, str] = {
        "DISB": "/api/v1/loans/p2p-disbursements/{biz}/status",
        "RPMT": "/api/v1/loans/p2p-repayments/{biz}/status",
        "DIST": "/api/v1/bank-funds/user-distributions/{biz}",
    }

    async def query_funds_status(
        self,
        *,
        tenant_id: str,
        request_id: str,
        biz_seq_no: str,
        biz_type: str,
    ) -> dict[str, Any]:
        """按业务类型路由到对应的 wedap 状态查询接口。

        wedap 无统一 /bank-funds/status；路径因 biz_type 而异（biz_type 对齐 lifecycel 真码）：
          - DISB → GET /api/v1/loans/p2p-disbursements/{bizSeqNo}/status
          - RPMT → GET /api/v1/loans/p2p-repayments/{bizSeqNo}/status
          - DIST → GET /api/v1/bank-funds/user-distributions/{bizSeqNo}
        不支持的 biz_type（如 COLL 归集）raise WedapError("UNSUPPORTED", ...)，调用方走降级路径。
        """
        tmpl = self._STATUS_PATH_TMPL.get(biz_type)
        if tmpl is None:
            raise WedapError("UNSUPPORTED", f"no status api for {biz_type}")
        path = tmpl.format(biz=biz_seq_no)
        return await self._get(
            path,
            tenant_id=tenant_id,
            request_id=request_id,
        )

    async def get_composite_steps(
        self,
        *,
        tenant_id: str,
        biz_seq_no: str,
    ) -> list[dict[str, Any]]:
        data = await self._get(
            f"/api/v1/composite-transactions/{biz_seq_no}/steps",
            tenant_id=tenant_id,
            request_id=f"steps-{biz_seq_no}",
        )
        return list(data.get("steps") or [])

    async def get_deposit_balance_total(
        self,
        *,
        tenant_id: str,
        request_id: str,
        params: dict[str, str],
    ) -> dict[str, Any]:
        # 契约 C 薄透传：原样转发调用方 query params（userId + wedap 必填 bizSeqNo/channelId 等）。
        return await self._get(
            "/api/v1/deposit/balances/total",
            tenant_id=tenant_id,
            request_id=request_id,
            params=params,
        )

    async def get_deposit_accounts(
        self,
        *,
        tenant_id: str,
        request_id: str,
        params: dict[str, str],
    ) -> dict[str, Any]:
        # 契约 C 薄透传：原样转发调用方 query params（userId + wedap 必填 bizSeqNo/channelId 等）。
        return await self._get(
            "/api/v1/deposit/accounts",
            tenant_id=tenant_id,
            request_id=request_id,
            params=params,
        )

    async def get_user_info(
        self,
        *,
        tenant_id: str,
        request_id: str,
        params: dict[str, str],
    ) -> dict[str, Any]:
        return await self._get(
            "/api/v1/users/info",
            tenant_id=tenant_id,
            request_id=request_id,
            params=params,
        )

    async def notify_batch_uploaded(self, *, payload: dict[str, Any]) -> dict[str, Any]:
        """通知 wedap 批次文件已上传 S3（flow-import，spec §5）。

        与南向资金接口不同契约：``apikey`` 头鉴权、响应非 ``{code,data}`` envelope，
        而是 ``{status, processingId, resultFilePath, message}``。
        payload 必填 dataType/channelId/importBatchNo/importDate/fileChecksum/fileSize，
        可选 payloadSchemaVersion/totalCount/replacesBatchNo。

        响应判定（ADR-0001 §外部错误契约，顺序敏感）：
          - ``success`` 字段为 ``false`` → 网关（APISIX）拒绝 → raise WedapGatewayRejected
            （唯一可靠锚点；含 401 key-auth 等 APISIX 内置/自定义插件拒绝）。
          - HTTP 401 且无 success 字段 → 上游应用层 401（apikey 未对齐）→ raise WedapError("401")。
          - 5xx → 服务端错误 → raise（可重试）。
          - 其余（200/400 业务响应）→ ``status`` 须属 7 值枚举则返回响应体；
            缺失 / 未识别 status → raise WedapError（不静默返回，否则会被上层记为 DELIVERED）。
        """
        # 自行序列化 body → 签名的字节与发送的字节完全一致（json= 会重新序列化，可能不一致）。
        body_bytes = json.dumps(payload, separators=(",", ":")).encode()
        headers = {
            "apikey": self._import_api_key or "",
            "Content-Type": "application/json",
        }
        # 切流到 APISIX 后必须带 X-Request-Id + HMAC 签名头（request-id-check / hmac-sign /
        # anti-replay 缺则 400）。签名密钥未配 = 直连 baffle 模式，不加签名头（向后兼容）。
        if self._import_signing_secret:
            timestamp = str(int(time.time()))
            headers["X-Request-Id"] = (
                f"wedap-import-{_header_safe(payload.get('importBatchNo', ''))}"
            )
            headers["X-Timestamp"] = timestamp
            headers["X-Nonce"] = uuid.uuid4().hex
            headers["X-Signature"] = _hmac_sign(
                self._import_signing_secret,
                method="POST",
                path=_IMPORT_PATH,
                timestamp=timestamp,
                body=body_bytes.decode(),
            )
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.post(
                f"{self._base}{_IMPORT_PATH}",
                content=body_bytes,
                headers=headers,
            )
        try:
            raw = r.json()
        except ValueError:
            raw = None  # 5xx / 非 JSON 错误体（如网关纯文本 503）
        # 仅 dict 体可解析；list / null / 标量一律视作无体（后续按 status 缺失处理）。
        body = raw if isinstance(raw, dict) else {}

        # 1) 网关拒绝唯一锚点：success 字段存在且为 false（APISIX/error-response 契约）。
        if body.get("success") is False:
            err = body.get("error")
            if not isinstance(
                err, dict
            ):  # error 可能是字符串/缺失 → 兜底空 dict，防 .get 抛 AttributeError
                err = {}
            raise WedapGatewayRejected(r.status_code, err.get("code"), err.get("message"))
        # 2) 上游应用层 401（无 success 字段，Spring 形态）→ apikey 未对齐。
        if r.status_code == 401:
            raise WedapError("401", "import apikey rejected")
        # 3) 服务端错误 → raise（可重试）。
        if r.status_code >= 500:
            r.raise_for_status()
        # 4) 业务响应：status 须属 7 值枚举才算合法受理响应；缺失 / 未识别 → 抛（不静默）。
        #    否则 body={} 或未知 status 会正常返回 → dispatch 记 DELIVERED → 误当受理成功。
        status = body.get("status")
        if status not in KNOWN_BATCH_STATUS:
            raise WedapError(
                "UNKNOWN_STATUS",
                f"unrecognized/missing wedap batch status: {status!r} "
                f"(batch={payload.get('importBatchNo')})",
            )
        return body
