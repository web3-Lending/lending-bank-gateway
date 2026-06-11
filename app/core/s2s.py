import hmac
import logging

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.envelope import err

logger = logging.getLogger(__name__)


class S2SMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app: object,
        *,
        secret: str | None,
        exempt_paths: set[str],
        allowed_callers: set[str] | None = None,
    ) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        # 空串与 None 等价：视为"未配置"，退化为 dev 模式（仅校验 caller 头，不校验 token）
        self._secret = secret or None
        self._exempt = exempt_paths
        # allowed_callers 为空集合或 None 表示不启用白名单校验
        # v1 共享 token 无法密码学绑定 caller，白名单兜底；v2 per-service token/签名绑定
        self._allowed_callers: set[str] | None = allowed_callers if allowed_callers else None

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # 尾斜杠规范化：/foo/ → /foo；根路径 "/" 保持不变
        path = request.url.path.rstrip("/") or "/"
        if path in self._exempt:
            return await call_next(request)
        trace_id = request.headers.get("X-Trace-Id", "trc-s2s")
        caller = request.headers.get("X-Caller-Service", "")
        if not caller:
            logger.warning(
                "s2s auth failed: path=%s reason=%s trace_id=%s",
                path,
                "missing_caller_service",
                trace_id,
            )
            return JSONResponse(
                err("GW_401_S2S", "missing X-Caller-Service", trace_id=trace_id), 401
            )
        if self._secret is not None:
            token = request.headers.get("X-S2S-Token", "")
            if not hmac.compare_digest(token, self._secret):
                # 禁止把 token 值写入日志
                logger.warning(
                    "s2s auth failed: path=%s reason=%s trace_id=%s",
                    path,
                    "bad_s2s_token",
                    trace_id,
                )
                return JSONResponse(err("GW_401_S2S", "bad s2s token", trace_id=trace_id), 401)
        # caller 白名单校验（secret 通过后执行；白名单为空=不启用）
        if self._allowed_callers is not None and caller not in self._allowed_callers:
            logger.warning(
                "s2s auth failed: path=%s reason=%s caller=%s trace_id=%s",
                path,
                "unknown_caller",
                caller,
                trace_id,
            )
            return JSONResponse(err("GW_401_S2S", "unknown caller", trace_id=trace_id), 401)
        return await call_next(request)
