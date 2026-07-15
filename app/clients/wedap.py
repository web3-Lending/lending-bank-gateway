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

# 受理类 status（幂等受理也算）；其余 5 值为业务拒绝类。受理类只信任 2xx 位（见
# notify_batch_uploaded 判定），dispatch 据此集合决定 DELIVERED / WedapBatchRejected。
ACCEPTED_BATCH_STATUS = frozenset({"ACCEPTED", "DUPLICATE_BATCH"})


class WedapError(Exception):
    def __init__(self, code: str, msg: str) -> None:
        super().__init__(f"wedap {code}: {msg}")
        self.code = code


def _error_text(body: dict[str, Any]) -> str:
    """错误文案兼容取 ``msg`` 或 ``message``。

    baffle 的 envelope 用 ``msg``；真 wedap（adapter AdapterResponse / gw-internal
    CommonResponse）用 ``message``。只认其一会把另一侧的错误文案丢成 "None"。
    空串 / 纯空白视同缺失（防 ``msg: " "`` 遮住有效 ``message``）；非字符串的
    truthy 载荷（数字码 / 结构化对象，部分 Java 网关会给）str 化保留，不丢诊断信息。
    """
    for key in ("msg", "message"):
        val = body.get(key)
        if isinstance(val, str):
            if val.strip():
                return val
            continue
        if val is not None:
            return str(val)
    return "<no error message>"


def _flow_import_error(body: dict[str, Any], *, fallback_code: str, detail: str) -> "WedapError":
    """flow-import 非受理响应 → WedapError 统一构造（notify / presign 共用）。

    gw-internal 网关错误统一形态 CommonResponse{code,message}（无 status）→ 用其
    ``code``；code 缺失或为 null → ``fallback_code`` 兜底（防 str(None) 污染
    调用方按 code 的分流与日志口径）。文案带上下文 detail 便于排障。
    """
    code = body.get("code")
    eff_code = str(code) if code is not None else fallback_code
    return WedapError(eff_code, f"{_error_text(body)} ({detail})")


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

    async def refund(
        self,
        *,
        tenant_id: str,
        request_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """退款（对接文档 v0.3.0 §4.7，wedap 已实现 adapter#73）：内部户 → 客户账户。

        契约要点（wedap 侧强制，gateway 薄透传不复刻）：oriBizSeqNo 关联被退款的原归集单；
        累计退款 ≤ 原单金额（FAILED 不计入）；currencyCode 须与原交易一致。
        """
        return await self._post(
            "/api/v1/transactions/refund",
            tenant_id=tenant_id,
            request_id=request_id,
            payload=payload,
        )

    async def query_transaction_status(
        self,
        *,
        tenant_id: str,
        request_id: str,
        ori_biz_seq_no: str,
        trans_type: str,
        ori_req_date: str,
    ) -> dict[str, Any]:
        """wedap 通用交易状态查询 `GET /api/v1/transactions/status`（对接文档 v0.3.0 §5.5）。

        替代已废弃的 per-type 状态端点（旧 DISB/RPMT/DIST 三桩是假实现，假单也回
        SUCCESS——W7；曾致 reconcile 假终态）。通用接口是真实现（2026-07-14 DEV 验证），
        四必填：bizSeqNo（本次查询流水号）+ transType（须等于提交值，wedap 按
        (oriBizSeqNo, transType) 消歧同号多行）+ oriReqDate（原单提交日 YYYYMMDD，
        bank_timezone）+ oriBizSeqNo。data.txnStatus ∈ PENDING/PROCESSING/SUCCESS/
        FAILED/REVERSED。COLL 归集同样支持（不再有 UNSUPPORTED 降级）。
        """
        # 查询自身流水号：gsq- 前缀 + 原单号截断，满足 ≤32 · [A-Za-z0-9_-] 硬校验
        query_biz = f"gsq-{ori_biz_seq_no}"[:32]
        return await self._get(
            "/api/v1/transactions/status",
            tenant_id=tenant_id,
            request_id=request_id,
            params={
                "bizSeqNo": query_biz,
                "channelId": "LEN",
                "transType": trans_type,
                "oriReqDate": ori_req_date,
                "oriBizSeqNo": ori_biz_seq_no,
            },
        )

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
        # 3) 合法枚举 status：受理类只信任 2xx 位——非成功位 + 受理体（如 403+ACCEPTED）
        #    是矛盾响应，返回会被 dispatch 记 DELIVERED（批次实际未受理，等 RESULT_OVERDUE
        #    才暴露）。拒绝类 status 任意非 401/5xx 位均保留（400/409/422 带业务拒因供
        #    排障与修复重传，不降级成泛化 HTTP 码）。
        status = body.get("status")
        if status in KNOWN_BATCH_STATUS:
            if status in ACCEPTED_BATCH_STATUS and not (200 <= r.status_code < 300):
                raise WedapError(
                    f"HTTP_{r.status_code}",
                    f"acceptance status {status!r} on non-2xx position not trusted "
                    f"(batch={payload.get('importBatchNo')})",
                )
            return body
        # 4) 非枚举响应：gw-internal CommonResponse{code,message} 用其 code，缺 code
        #    兜底 UNKNOWN_STATUS（不静默返回，否则被上层记 DELIVERED 误当受理成功）。
        raise _flow_import_error(
            body,
            fallback_code="UNKNOWN_STATUS",
            detail=f"status={status!r} http={r.status_code} batch={payload.get('importBatchNo')}",
        )

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
            raise _flow_import_error(
                body,
                fallback_code=f"HTTP_{r.status_code}",
                detail=f"op={operation} batch={import_batch_no}",
            )
        # 4) 业务响应：status 须为 OK 且带非空 url，否则抛（不静默返回空 url）。
        status = body.get("status")
        url = body.get("url")
        if status != "OK" or not isinstance(url, str) or not url:
            if "status" not in body and "code" in body:
                # gw-internal 网关错误统一形态 CommonResponse{code,message}（无 status）。
                raise _flow_import_error(
                    body,
                    fallback_code="PRESIGN_FAILED",
                    detail=f"op={operation} batch={import_batch_no}",
                )
            raise WedapError(
                "PRESIGN_FAILED",
                f"presign not OK: status={status!r} url={url!r} "
                f"(op={operation} batch={import_batch_no})",
            )
        return url
