from typing import Any

import httpx


class WedapError(Exception):
    def __init__(self, code: str, msg: str) -> None:
        super().__init__(f"wedap {code}: {msg}")
        self.code = code


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
    ) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._import_api_key = import_api_key

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

        HTTP 状态处理：
          - 200/400 → 返回响应体（``status`` 字段载明 ACCEPTED/DUPLICATE_BATCH/
            CHECKSUM_MISMATCH/INVALID_PARAM 等，调用方据此决定重传/修复，**不抛**）。
          - 401 → apikey 失败 → raise WedapError("401")。
          - 5xx → 服务端错误 → raise（可重试）。
        """
        headers = {
            "apikey": self._import_api_key or "",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.post(
                f"{self._base}/bank/api/v1/import/batch-uploaded",
                json=payload,
                headers=headers,
            )
        if r.status_code == 401:
            raise WedapError("401", "import apikey rejected")
        if r.status_code >= 500:
            r.raise_for_status()
        return dict(r.json())
