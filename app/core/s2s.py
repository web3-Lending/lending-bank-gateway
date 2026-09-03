import hmac
import logging
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.context import current_ids
from app.core.envelope import err

if TYPE_CHECKING:  # pragma: no cover
    from app.core.svc_jwt import BffSvcJwtVerifier

logger = logging.getLogger(__name__)

# API-HTTP-004 + §7.4：401 必须带适用的 `WWW-Authenticate`。challenge 点名本仓
# 真正接受的凭证，调用方据此知道该刷新哪一种，而不是盲目重试。
# S2S 面接受**两种**凭证，故按 RFC 7235 §4.1 并列两个 challenge：
#   - `S2S`（header `X-S2S-Token`）：lending 服务间直连的共享 secret / 专属 token
#   - `Bearer`：经 BFF `/internal/proxy` 进来的 svc JWT（BFF 签，aud=bank-gateway）
# wedap 入站回调面另用 `apikey`，见下。
_WWW_AUTHENTICATE_S2S = (
    'S2S realm="lending-bank-gateway", header="X-S2S-Token", '
    'Bearer realm="lending-bank-gateway", audience="bank-gateway"'
)
_WWW_AUTHENTICATE_APIKEY = 'ApiKey realm="lending-bank-gateway", header="apikey"'


def _unauthorized(code: str, message: str, *, trace_id: str, challenge: str) -> JSONResponse:
    """构造 401：统一带 `WWW-Authenticate`。

    收成一个出口而不是 5 处各写各的——漏一处就等于该条认证路径静默不合规，
    而 401 分散在 callback / caller / per-service token / 共享 secret / 白名单
    五条互不相邻的分支上。
    """
    return JSONResponse(
        err(code, message, trace_id=trace_id),
        status_code=401,
        headers={"WWW-Authenticate": challenge},
    )


def parse_caller_tokens(raw: str) -> dict[str, str] | None:
    """解析 GW_S2S_CALLER_TOKENS（`caller1:token1,caller2:token2`）。

    唯一权威解析：create_app 的中间件装配与 admin-caller fail-fast 校验共用本函数，
    防止两处解析漂移（codex R2 P0：`fund-ops:` 空 token 曾被 fail-fast 误判为已绑定，
    而运行时丢弃空 token 回退共享 secret → 绑定形同虚设）。
    空段/空 token/空名字一律丢弃；结果为空 → None（未启用）。
    """
    tokens: dict[str, str] = {}
    for pair in raw.split(","):
        if ":" not in pair:
            continue
        name, _, tok = pair.partition(":")
        name, tok = name.strip(), tok.strip()
        if name and tok:
            tokens[name] = tok
    return tokens or None


def _bearer_credential(header_value: str) -> str:
    """从 `Authorization` 头里取出 Bearer 凭证；不是 Bearer 就返回空串。

    scheme 按 RFC 7235 大小写不敏感。返回空串 = 本请求没有出示 Bearer 凭证，
    由调用处决定后续（不是"验证失败"，两者的 401 措辞不同）。
    """
    scheme, _, credential = header_value.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return credential.strip()


class S2SMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app: object,
        *,
        secret: str | None,
        exempt_paths: set[str],
        allowed_callers: set[str] | None = None,
        caller_tokens: dict[str, str] | None = None,
        callback_paths: set[str] | None = None,
        callback_api_key: str | None = None,
        svc_jwt_verifier: "BffSvcJwtVerifier | None" = None,
    ) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        # 空串与 None 等价：视为"未配置"，退化为 dev 模式（仅校验 caller 头，不校验 token）
        self._secret = secret or None
        self._exempt = exempt_paths
        # allowed_callers 为空集合或 None 表示不启用白名单校验
        # 共享 token 无法密码学绑定 caller，白名单兜底
        self._allowed_callers: set[str] | None = allowed_callers if allowed_callers else None
        # per-service token（A-m-002）：{caller: token}。配置则优先，按 caller 专属 token 校验，
        # 把 caller 与 token 密码学绑定——某 caller 的 token 泄露只能冒充它自己，不能伪造他人。
        self._caller_tokens: dict[str, str] | None = caller_tokens if caller_tokens else None
        # wedap→gateway 入站回调 path（FU-GW-INBOUND-AUTH-WEDAP-CALLBACK）：外部 wedap 无
        # lending S2S token，这些 path 改用 `apikey` header 认证（在 middleware 层、body 解析前，
        # 与 S2S 同级）。callback_api_key 为空=dev 降级放行（非 local/test 环境由 create_app
        # 启动期 fail-fast 兜底，保证 prod/dev-hw 必配）。
        self._callback_paths: set[str] = callback_paths or set()
        self._callback_api_key: str | None = callback_api_key or None
        # BFF svc JWT 验签器（collab brj8tl7t90a7i5n169g69u23）：经 BFF
        # `/internal/proxy/bank-gateway/...` 进来的请求带的是 `Authorization: Bearer
        # <RS256 svc JWT>`，**不带** X-S2S-Token（BFF 出站透传白名单里没有这个头，
        # 调用方也补不了）。None = 未配置 GW_BFF_BASE_URL，该通路整体不启用。
        self._svc_jwt_verifier = svc_jwt_verifier

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # 尾斜杠规范化：/foo/ → /foo；根路径 "/" 保持不变
        path = request.url.path.rstrip("/") or "/"
        if path in self._exempt:
            return await call_next(request)
        # API-HTTP-013：必须与 IdentifierMiddleware（本中间件的外层）产出的、已过校验
        # 的 trace_id 同源。此前读原始 header 并回落字面量 "trc-s2s"，导致所有未携带
        # X-Trace-Id 的 401 响应体共用同一个假 trace_id，与响应头和日志里的真值对不上。
        trace_id = current_ids().trace_id

        # wedap→gateway 入站回调：用 apikey 认证（body 解析前，middleware 层），不走 S2S token。
        if path in self._callback_paths:
            if not self._callback_api_key:
                # 空 key = dev 降级放行；非 local/test 环境已在 create_app 启动期 fail-fast，
                # 故走到这里必是 local/test。
                logger.warning(
                    "wedap callback auth degraded (dev): no callback api key, path=%s trace_id=%s",
                    path,
                    trace_id,
                )
                return await call_next(request)
            provided = request.headers.get("apikey", "")
            if not provided or not hmac.compare_digest(provided, self._callback_api_key):
                # 禁止把 apikey 值写入日志
                logger.warning(
                    "callback auth failed: path=%s reason=%s trace_id=%s",
                    path,
                    "invalid_or_missing_apikey",
                    trace_id,
                )
                return _unauthorized(
                    "GW_401_CALLBACK",
                    "invalid or missing apikey",
                    trace_id=trace_id,
                    challenge=_WWW_AUTHENTICATE_APIKEY,
                )
            return await call_next(request)

        caller = request.headers.get("X-Caller-Service", "")
        if not caller:
            logger.warning(
                "s2s auth failed: path=%s reason=%s trace_id=%s",
                path,
                "missing_caller_service",
                trace_id,
            )
            return _unauthorized(
                "GW_401_S2S",
                "missing X-Caller-Service",
                trace_id=trace_id,
                challenge=_WWW_AUTHENTICATE_S2S,
            )
        token = request.headers.get("X-S2S-Token", "")

        # 模式一（优先）：per-service token——按 caller 专属 token 校验，密码学绑定 caller↔token。
        # 混合语义（codex R2 P1）：只有「在 token 表里的 caller」强制走专属 token；
        # 不在表里的 caller 落回模式二共享 secret（存量 caller 增量迁移，不被一刀切 401）。
        # request.state.s2s_token_bound 标记本请求是否凭专属 token 认证——admin 配置面
        # （platform_accounts）只信 token_bound 请求，共享 secret 冒充 caller 头到不了 admin。
        request.state.s2s_token_bound = False

        # 模式零：经 BFF 的 svc JWT。**仅在没有 X-S2S-Token 时**尝试——带了
        # X-S2S-Token 的请求是在明示"我用直连凭证"，那就照直连规则判它的成败，
        # 不给它一条"这个不行就换那个"的旁路（凭证择优会让一处泄露即全面失效）。
        if not token and self._svc_jwt_verifier is not None:
            bearer = _bearer_credential(request.headers.get("Authorization", ""))
            if bearer:
                claims = await self._svc_jwt_verifier.verify(bearer)
                if claims is None:
                    # 具体拒因（alg/kid/aud/exp/签名）已由验签器写审计日志；对外
                    # 只回一句话，不回显 token 结构细节。
                    logger.warning(
                        "s2s auth failed: path=%s reason=%s caller=%s trace_id=%s",
                        path,
                        "bad_bff_svc_jwt",
                        caller,
                        trace_id,
                    )
                    return _unauthorized(
                        "GW_401_S2S",
                        "bad bff svc token",
                        trace_id=trace_id,
                        challenge=_WWW_AUTHENTICATE_S2S,
                    )
                # 签名值 vs 头值对账：token 里的 caller_service / tenant_id 是 BFF
                # 签过名的（改一个字节签名就不对），而同名的请求头是明文、可篡改。
                # 两者本该恒等——BFF 出站时 caller 头与 caller_service claim 同取
                # IP-ACL 解析出的 caller_name，x-tenant-id 头与 tenant_id claim 同取
                # 入站那一个头值（lending-console-bff/app/internal_proxy/router.py
                # :245-283）。所以不等只有两种可能：有人拿着张真 token 改了头，或者
                # 上游契约变了。两种都不该放行——手里有签名过的真值却按明文头做判断，
                # 等于白验了这个签名。claim 缺席时不强求（BFF 只在有值时写入）。
                claim_caller = claims.get("caller_service")
                if claim_caller is not None and claim_caller != caller:
                    logger.warning(
                        "s2s auth failed: path=%s reason=%s caller=%s claim_caller=%s "
                        "trace_id=%s",
                        path,
                        "caller_header_claim_mismatch",
                        caller,
                        claim_caller,
                        trace_id,
                    )
                    return _unauthorized(
                        "GW_401_S2S",
                        "caller does not match svc token",
                        trace_id=trace_id,
                        challenge=_WWW_AUTHENTICATE_S2S,
                    )
                claim_tenant = claims.get("tenant_id")
                header_tenant = current_ids().tenant_id
                # 租户头缺席交给业务层的 require_headers 报 400（错因更准），这里
                # 只拦「两个值都在但对不上」——那是越权到别人租户的形状。
                if (
                    claim_tenant is not None
                    and header_tenant is not None
                    and claim_tenant != header_tenant
                ):
                    logger.warning(
                        "s2s auth failed: path=%s reason=%s caller=%s trace_id=%s",
                        path,
                        "tenant_header_claim_mismatch",
                        caller,
                        trace_id,
                    )
                    return _unauthorized(
                        "GW_401_S2S",
                        "tenant does not match svc token",
                        trace_id=trace_id,
                        challenge=_WWW_AUTHENTICATE_S2S,
                    )
                # 白名单在本通路**照查**（与专属 token 通路的"绑定即免查"不同）：
                # 专属 token 是本仓运维一个个发出去的，发放动作本身即授权；而 svc JWT
                # 是 BFF 发的，谁能拿到 aud=bank-gateway 由 BFF 侧 ACL 决定。本仓保留
                # 自己那份「谁准打进来」的名单，不把准入判断整体外包出去。
                # 走到这里 caller 已与签名值对上，白名单判的就是签名过的身份。
                if self._allowed_callers is not None and caller not in self._allowed_callers:
                    logger.warning(
                        "s2s auth failed: path=%s reason=%s caller=%s trace_id=%s",
                        path,
                        "unknown_caller",
                        caller,
                        trace_id,
                    )
                    return _unauthorized(
                        "GW_401_S2S",
                        "unknown caller",
                        trace_id=trace_id,
                        challenge=_WWW_AUTHENTICATE_S2S,
                    )
                # s2s_token_bound 保持 False：admin 配置面（platform_accounts）只认
                # 本仓自己签发的专属 token。svc JWT 的 caller_service claim 虽由 BFF
                # 签名保护，但「哪些 caller 能改资金白名单」这条授权仍应由本仓的
                # GW_S2S_CALLER_TOKENS 独立决定，不随 BFF 的签发策略漂移
                # （create_app 的 GW_ADMIN_CALLERS↔专属 token fail-fast 因此仍成立）。
                return await call_next(request)

        if self._caller_tokens is not None:
            expected = self._caller_tokens.get(caller)
            if expected is not None:
                if not hmac.compare_digest(token, expected):
                    # 禁止把 token 值写入日志
                    logger.warning(
                        "s2s auth failed: path=%s reason=%s caller=%s trace_id=%s",
                        path,
                        "bad_per_service_token",
                        caller,
                        trace_id,
                    )
                    return _unauthorized(
                        "GW_401_S2S",
                        "bad s2s token",
                        trace_id=trace_id,
                        challenge=_WWW_AUTHENTICATE_S2S,
                    )
                # token 已绑定 caller，无需再查白名单
                request.state.s2s_token_bound = True
                return await call_next(request)

        # 模式二（回退）：共享 secret + 可选白名单
        if self._secret is not None:
            if not hmac.compare_digest(token, self._secret):
                logger.warning(
                    "s2s auth failed: path=%s reason=%s trace_id=%s",
                    path,
                    "bad_s2s_token",
                    trace_id,
                )
                return _unauthorized(
                    "GW_401_S2S",
                    "bad s2s token",
                    trace_id=trace_id,
                    challenge=_WWW_AUTHENTICATE_S2S,
                )
        # caller 白名单校验（secret 通过后执行；白名单为空=不启用）
        if self._allowed_callers is not None and caller not in self._allowed_callers:
            logger.warning(
                "s2s auth failed: path=%s reason=%s caller=%s trace_id=%s",
                path,
                "unknown_caller",
                caller,
                trace_id,
            )
            return _unauthorized(
                "GW_401_S2S",
                "unknown caller",
                trace_id=trace_id,
                challenge=_WWW_AUTHENTICATE_S2S,
            )
        return await call_next(request)
