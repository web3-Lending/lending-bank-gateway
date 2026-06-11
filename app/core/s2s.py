import hmac

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.envelope import err


class S2SMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: object, *, secret: str | None, exempt_paths: set[str]) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._secret = secret
        self._exempt = exempt_paths

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in self._exempt:
            return await call_next(request)
        trace_id = request.headers.get("X-Trace-Id", "trc-s2s")
        if not request.headers.get("X-Caller-Service"):
            return JSONResponse(
                err("GW_401_S2S", "missing X-Caller-Service", trace_id=trace_id), 401
            )
        if self._secret is not None:
            token = request.headers.get("X-S2S-Token", "")
            if not hmac.compare_digest(token, self._secret):
                return JSONResponse(err("GW_401_S2S", "bad s2s token", trace_id=trace_id), 401)
        return await call_next(request)
