"""BFF 签发的服务间 JWT（svc JWT）入站本地验签。

**为什么本仓需要它**：调用方经 BFF `/internal/proxy/bank-gateway/...` 打进来时，
凭证是 `Authorization: Bearer <RS256 svc JWT>`——BFF 用租户私钥自签、把按 IP-ACL
识别出的调用方身份写进 `caller_service` claim。BFF 的出站透传白名单里**没有**
`X-S2S-Token`，所以那条链路上永远不会出现本仓原有的共享 secret / per-service token。
本模块补的就是这条通路的验证能力（collab brj8tl7t90a7i5n169g69u23）。

**做法**：从 BFF 的 SVC-JWKS 端点拉公钥并按 TTL 缓存，本地验 RS256 签名 + aud + iss
+ exp。不做逐请求的 RPC 回源——JWKS 有缓存，BFF 短暂抖动不至于把资金端点认证打死。

**GUARDRAIL：fail-closed**。凡是无法**正面确认**的 token 一律返回 None（调用方据此
拒绝）：空 token、头部畸形、alg 非 RS256、无 kid、kid 不在 JWKS、JWK 解不出公钥、
签名/aud/iss/exp 任一不过、JWKS 拉不到且缓存已过期——没有任何一条通向放行。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx
import jwt as pyjwt
from jwt.algorithms import RSAAlgorithm

logger = logging.getLogger(__name__)
_audit = logging.getLogger("lending_auth.audit")

#: JWKS 拉取失败后的静默期。失败即清空缓存（fail-closed），若不退避则每个请求都会
#: 重新打 BFF——BFF 一挂就变成本仓对它的拉取风暴。
_FETCH_RETRY_BACKOFF_SECONDS = 5.0


class BffSvcJwtVerifier:
    """BFF svc JWT 的本地验签器。一个实例持有一份 JWKS 缓存。

    做成实例而非模块级全局：配置由 create_app 注入（与本仓 Settings 注入范式一致），
    且多个测试用例之间不会共享缓存（全局缓存串味是这类模块最常见的假绿来源）。
    """

    def __init__(
        self,
        *,
        jwks_url: str,
        audience: str,
        issuer: str,
        cache_ttl_seconds: float,
        leeway_seconds: float,
        timeout_seconds: float,
    ) -> None:
        self._jwks_url = jwks_url
        self._audience = audience
        self._issuer = issuer
        self._cache_ttl_seconds = cache_ttl_seconds
        self._leeway_seconds = leeway_seconds
        self._timeout_seconds = timeout_seconds
        self._keys: dict[str, dict[str, Any]] = {}
        self._keys_expire_at: float = 0.0
        # singleflight：TTL 到期时只让一个协程去拉，其余等待后复用新缓存。
        self._refresh_lock = asyncio.Lock()

    async def _fetch_keys(self) -> dict[str, dict[str, Any]]:
        """GET SVC-JWKS，返回 {kid: jwk}。

        `X-Caller-Service: internal` 是 BFF `/internal/*` 的入口要求，缺了会被 BFF
        以 400 挡掉（lending-core 侧已实证的同一契约）。
        """
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout_seconds), trust_env=False
        ) as client:
            resp = await client.get(
                self._jwks_url, headers={"X-Caller-Service": "internal"}
            )
        resp.raise_for_status()
        payload: dict[str, Any] = resp.json()
        keys: list[dict[str, Any]] = payload.get("keys") or []
        return {k["kid"]: k for k in keys if k.get("kid")}

    async def _jwk_for(self, kid: str) -> dict[str, Any] | None:
        """按 kid 取 JWK，TTL 到期才回源。

        TTL 内的 kid miss **不**触发回源：伪造/轮换后的陌生 kid 否则就是一个免费的
        打 BFF 的杠杆。密钥轮换最迟在下个 TTL 被接上。
        """
        now = time.monotonic()
        if now >= self._keys_expire_at:
            async with self._refresh_lock:
                now = time.monotonic()
                if now >= self._keys_expire_at:
                    try:
                        self._keys = await self._fetch_keys()
                        self._keys_expire_at = now + self._cache_ttl_seconds
                    except Exception as exc:  # 网络错误 / 非 2xx / 非法 JSON
                        # 丢掉过期缓存而不是续用：已轮换/吊销的公钥不能在 JWKS 不可达
                        # 期间继续验过 token。空缓存 → None → 上游拒绝。
                        logger.warning("svc-jwks refresh failed, backing off: %s", exc)
                        self._keys = {}
                        self._keys_expire_at = now + _FETCH_RETRY_BACKOFF_SECONDS
        return self._keys.get(kid)

    async def verify(self, token: str) -> dict[str, Any] | None:
        """验一个 svc JWT：确认得了返 claims，否则返 None（fail-closed）。"""
        if not token:
            _audit.warning("event=svc_jwt.reject reason=%s", "empty_token")
            return None
        try:
            header = pyjwt.get_unverified_header(token)
        except pyjwt.PyJWTError:
            _audit.warning("event=svc_jwt.reject reason=%s", "malformed_header")
            return None
        if header.get("alg") != "RS256":
            # 只认 RS256。放开 alg 等于把 alg=none / HS256（用公钥当 HMAC 密钥）
            # 这类经典绕过一并放进来。
            _audit.warning(
                "event=svc_jwt.reject reason=%s alg=%s", "alg_not_rs256", header.get("alg")
            )
            return None
        kid = header.get("kid")
        if not kid:
            _audit.warning("event=svc_jwt.reject reason=%s", "missing_kid")
            return None
        jwk = await self._jwk_for(kid)
        if jwk is None:
            _audit.warning("event=svc_jwt.reject reason=%s kid=%s", "kid_not_in_jwks", kid)
            return None
        try:
            public_key = RSAAlgorithm.from_jwk(json.dumps(jwk))
        except Exception as exc:
            logger.warning("svc-jwks bad jwk for kid %s: %s", kid, exc)
            return None
        try:
            claims: dict[str, Any] = pyjwt.decode(
                token,
                public_key,  # type: ignore[arg-type]
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway_seconds,
                # require：这三个 claim 必须**存在**，不只是"存在时合法"。PyJWT 默认在
                # exp 缺席时直接跳过时效校验——那样一个没有 exp 的 token 就永不过期。
                options={"require": ["exp", "aud", "iss"]},
            )
        except pyjwt.PyJWTError as exc:
            _audit.warning(
                "event=svc_jwt.reject reason=%s kid=%s", type(exc).__name__, kid
            )
            return None
        _audit.info(
            "event=svc_jwt.verified kid=%s sub=%s caller_service=%s jti=%s",
            kid,
            claims.get("sub"),
            claims.get("caller_service"),
            claims.get("jti"),
        )
        return claims
