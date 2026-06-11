import hmac
import logging

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.envelope import err

logger = logging.getLogger(__name__)


class S2SMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: object, *, secret: str | None, exempt_paths: set[str]) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        # 空串与 None 等价：视为"未配置"，退化为 dev 模式（仅校验 caller 头，不校验 token）
        self._secret = secret or None
        self._exempt = exempt_paths

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # 尾斜杠规范化：/foo/ → /foo；根路径 "/" 保持不变
        path = request.url.path.rstrip("/") or "/"
        if path in self._exempt:
            return await call_next(request)
        trace_id = request.headers.get("X-Trace-Id", "trc-s2s")
        if not request.headers.get("X-Caller-Service"):
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
        return await call_next(request)
