import json
import re
from typing import Any

import httpx

# flow-import notify 端点（web2-core BatchUploadedNotificationController 自带 /bank 前缀；
# 经 gw-internal /external/web2-core 路由或直连 web2-core 均为此路径）。
_IMPORT_PATH = "/bank/api/v1/import/batch-uploaded"
# flow-import presign 签发端点（web2-core PresignController，P4 预签名投递）。
_PRESIGN_PATH = "/bank/api/v1/import/presign"


def _header_safe(value: object, *, max_len: int = 80) -> str:
    """把业务号清洗成 header-safe 值（防 CR/LF/非 ASCII 造成非法 header / 注入）。

    非 ``[A-Za-z0-9._:-]`` 一律替换为 ``-``，并截断到 ``max_len``。正常业务号
    （如 ``BATCH-LEN-20260624-001``）保持原样，异常输入不再进 header 原文。
    """
    return re.sub(r"[^A-Za-z0-9._:-]", "-", str(value))[:max_len]


# web2-core BatchUploadedResponse.Status 的 7 值枚举（外部错误契约）。
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


def _error_text(body: dict[str, Any]) -> str:
    """错误文案兼容取 ``msg`` 或 ``message``。

    baffle 的 envelope 用 ``msg``；真 wedap（adapter AdapterResponse / gw-internal
    CommonResponse）用 ``message``。只认其一会把另一侧的错误文案丢成 "None"。
    空串 / 纯空白 / 非字符串视同缺失（防 ``msg: " "`` 遮住有效 ``message``）。
    """
    for key in ("msg", "message"):
        val = body.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return "<no error message>"


class WedapClient:
    """南向 client：所有外呼带 timeout（规范07 §1），写操作不自动重试。

    幂等靠上层 RESULT_UNKNOWN 收敛。

    对接形态（2026-07 起走 wedap gw-internal，Phase 1 无网关鉴权）：

    - 银行南向（放款/还款/归集/分发/状态/查询）：``base_url`` 直拼原始路径。
      经 gw-internal 时 base_url 自带 ``/lending-gw`` 前缀
      （如 ``http://<gw-internal>:8000/lending-gw``）；直连 baffle 时为
      ``http://baffle:8021``。两种形态请求头一致（X-Tenant-Id / X-Request-Id）。
    - flow-import（notify / presign）：``import_base_url`` 直拼
      ``/bank/api/v1/import/*``（web2-core controller 自带 /bank 前缀）。经
      gw-internal 时自带 ``/external/web2-core`` 前缀；鉴权是 web2-core 应用层
      ``apikey``（FLOW_IMPORT_API_KEYS 白名单），与网关无关。
    - gw-internal Phase 2 的 lending 鉴权（app-JWT）尚未启用；启用时按彼时契约实现。
    """

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        import_api_key: str | None = None,
        import_base_url: str | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._import_api_key = import_api_key
        # flow-import 独立 base：银行南向与 import 经 gw-internal 的路由前缀不同
        # （/lending-gw vs /external/web2-core），一个 base 罩不住。空 = 回落
        # base_url（local/test 场景，import 链路默认关闭）。
        self._import_base = (import_base_url or base_url).rstrip("/")

    def _headers(self, tenant_id: str, request_id: str) -> dict[str, str]:
        return {
            "X-Tenant-Id": tenant_id,
            "X-Request-Id": request_id,
            "Content-Type": "application/json",
        }

    def _import_headers(self, *, request_id: str) -> dict[str, str]:
        """flow-import（notify / presign）请求头：应用层 apikey 鉴权 + 追踪头。"""
        return {
            "apikey": self._import_api_key or "",
            "Content-Type": "application/json",
            "X-Request-Id": request_id,
        }

    async def _post(
        self,
        path: str,
        *,
        tenant_id: str,
        request_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        # 紧凑序列化（无空格），与既有落盘/重放字节形态保持一致。
        body_bytes = json.dumps(payload, separators=(",", ":")).encode()
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.post(
                f"{self._base}{path}",
                content=body_bytes,
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
            raise WedapError(str(body.get("code")), _error_text(body))
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

        与南向资金接口不同契约：``apikey`` 头鉴权（web2-core 应用层）、响应非
        ``{code,data}`` envelope，而是 ``{status, processingId, resultFilePath, message}``。
        payload 必填 dataType/channelId/importBatchNo/importDate/fileChecksum/fileSize，
        可选 payloadSchemaVersion/totalCount/replacesBatchNo。

        响应判定（外部错误契约 · gw-internal 修订版，顺序敏感）：
          - HTTP 401 → apikey 未对齐（web2-core 应用层拒绝）→ raise WedapError("401")。
          - 5xx → 服务端 / 网关转发错误 → raise（可重试）。
          - 其余（200/400 业务响应）→ ``status`` 须属 7 值枚举则返回响应体；
            body 无 ``status`` 但有 ``code``（gw-internal GlobalErrorWebExceptionHandler
            的 CommonResponse{code,message} 网关错误形态）→ 按其 code/message 抛；
            其余缺失 / 未识别 status → raise WedapError（不静默返回，否则会被
            dispatch_delivery_once 记为 DELIVERED → 误当受理成功）。
        """
        # 紧凑序列化，与南向 _post 字节形态一致。
        body_bytes = json.dumps(payload, separators=(",", ":")).encode()
        headers = self._import_headers(
            request_id=f"wedap-import-{_header_safe(payload.get('importBatchNo', ''))}",
        )
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.post(
                f"{self._import_base}{_IMPORT_PATH}",
                content=body_bytes,
                headers=headers,
            )
        try:
            raw = r.json()
        except ValueError:
            raw = None  # 5xx / 非 JSON 错误体（如网关纯文本 503）
        # 仅 dict 体可解析；list / null / 标量一律视作无体（后续按 status 缺失处理）。
        body = raw if isinstance(raw, dict) else {}

        # 1) 401 → apikey 未对齐（web2-core 应用层 ApiKeyAuthInterceptor 拒绝）。
        if r.status_code == 401:
            raise WedapError("401", "import apikey rejected")
        # 2) 服务端 / 网关转发错误 → raise（可重试）。
        if r.status_code >= 500:
            r.raise_for_status()
        # 3) 业务响应仅允许 200/400（web2-core 契约）；其余 4xx（网关限流 / 路由错误等）
        #    即使体里带 status 也不可信 → 按 CommonResponse code 抛，缺 code 用 HTTP 码兜底。
        if r.status_code not in (200, 400):
            raise WedapError(str(body.get("code") or f"HTTP_{r.status_code}"), _error_text(body))
        # 4) status 须属 7 值枚举才算合法受理响应。
        status = body.get("status")
        if status not in KNOWN_BATCH_STATUS:
            if "status" not in body and "code" in body:
                # gw-internal 网关错误统一形态 CommonResponse{code,message}（无 status）。
                raise WedapError(str(body.get("code")), _error_text(body))
            raise WedapError(
                "UNKNOWN_STATUS",
                f"unrecognized/missing wedap batch status: {status!r} "
                f"(batch={payload.get('importBatchNo')})",
            )
        return body

    async def request_presign(
        self,
        *,
        operation: str,
        data_type: str,
        channel_id: str,
        import_date: str,
        import_batch_no: str,
    ) -> str:
        """向 web2-core 申请 presigned URL（P4 预签名投递：lending 无需长期 wedap S3 凭证）。

        POST /bank/api/v1/import/presign，复用 flow-import 应用层 ``apikey`` 鉴权（同
        notify）。operation="UPLOAD" 拿投递上传 URL（PUT），"RESULT" 拿结果读取 URL（GET）。
        契约响应：``{status:"OK", operation, method, url, objectKey, expiresInSeconds}``；返回
        解析出的 ``url``。

        判定顺序对齐 notify_batch_uploaded：
          - HTTP 401 → apikey 未对齐 → raise WedapError("401")。
          - 5xx → raise（可重试）。
          - 200 但 status != "OK" / 缺 url → raise（网关 CommonResponse{code,message}
            错误形态优先按其 code/message 抛；不静默返回空 url）。
        """
        payload: dict[str, Any] = {
            "operation": operation,
            "dataType": data_type,
            "channelId": channel_id,
            "importDate": import_date,
            "importBatchNo": import_batch_no,
        }
        body_bytes = json.dumps(payload, separators=(",", ":")).encode()
        headers = self._import_headers(
            request_id=f"wedap-presign-{operation}-{_header_safe(import_batch_no)}",
        )
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.post(
                f"{self._import_base}{_PRESIGN_PATH}",
                content=body_bytes,
                headers=headers,
            )
        try:
            raw = r.json()
        except ValueError:
            raw = None  # 5xx / 非 JSON 错误体
        body = raw if isinstance(raw, dict) else {}

        # 1) 401 → apikey 未对齐。
        if r.status_code == 401:
            raise WedapError("401", "import apikey rejected")
        # 2) 服务端 / 网关转发错误 → raise（可重试）。
        if r.status_code >= 500:
            r.raise_for_status()
        # 3) presign 成功响应只认 2xx；非 2xx（网关限流等）即使体里带 status=OK 也不可信。
        if not (200 <= r.status_code < 300):
            raise WedapError(str(body.get("code") or f"HTTP_{r.status_code}"), _error_text(body))
        # 4) 业务响应：status 须为 OK 且带非空 url，否则抛（不静默返回空 url）。
        status = body.get("status")
        url = body.get("url")
        if status != "OK" or not isinstance(url, str) or not url:
            if "status" not in body and "code" in body:
                # gw-internal 网关错误统一形态 CommonResponse{code,message}（无 status）。
                raise WedapError(str(body.get("code")), _error_text(body))
            raise WedapError(
                "PRESIGN_FAILED",
                f"presign not OK: status={status!r} url={url!r} "
                f"(op={operation} batch={import_batch_no})",
            )
        return url
