"""BFF svc JWT 本地验签器的规格。

每条用例钉住一种「拿不准就拒」的形态——验签器唯一能返回 claims 的路径是**全部**
检查都正面通过；其余一律 None（上游据此 401）。
"""

import asyncio
import json
import time
from typing import Any

import httpx
import jwt as pyjwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from app.core.svc_jwt import BffSvcJwtVerifier

JWKS_URL = "http://bff:7018/internal/svc-jwks.json"
AUDIENCE = "bank-gateway"
ISSUER = "lending-console-bff"


def _keypair() -> Any:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


# 模块级两把钥匙：一把是"BFF 的"，另一把用来伪造签名。keygen 不便宜，只做一次。
_BFF_KEY = _keypair()
_ROGUE_KEY = _keypair()


def _jwk(private_key: Any, kid: str) -> dict[str, Any]:
    jwk: dict[str, Any] = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk["kid"] = kid
    return jwk


def _token(
    private_key: Any,
    *,
    kid: str = "WBTHK01-1",
    audience: str = AUDIENCE,
    issuer: str = ISSUER,
    expires_in: int = 300,
    caller_service: str = "lifecycle",
    drop: tuple[str, ...] = (),
    algorithm: str = "RS256",
) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": issuer,
        "aud": audience,
        "sub": "bff-service",
        "tenant_id": "WBTHK01",
        "caller_service": caller_service,
        "jti": "jti-1",
        "iat": now,
        "exp": now + expires_in,
    }
    for claim in drop:
        payload.pop(claim, None)
    return pyjwt.encode(payload, private_key, algorithm=algorithm, headers={"kid": kid})


def _verifier(**overrides: Any) -> BffSvcJwtVerifier:
    kwargs: dict[str, Any] = {
        "jwks_url": JWKS_URL,
        "audience": AUDIENCE,
        "issuer": ISSUER,
        "cache_ttl_seconds": 300.0,
        "leeway_seconds": 30.0,
        "timeout_seconds": 5.0,
    }
    kwargs.update(overrides)
    return BffSvcJwtVerifier(**kwargs)


def _mock_jwks(*jwks: dict[str, Any]) -> respx.Route:
    return respx.get(JWKS_URL).mock(
        return_value=httpx.Response(200, json={"keys": list(jwks)})
    )


@respx.mock
async def test_valid_token_returns_claims() -> None:
    _mock_jwks(_jwk(_BFF_KEY, "WBTHK01-1"))
    claims = await _verifier().verify(_token(_BFF_KEY))
    assert claims is not None
    assert claims["caller_service"] == "lifecycle"
    assert claims["tenant_id"] == "WBTHK01"


@respx.mock
async def test_wrong_audience_rejected() -> None:
    """BFF 发给别的下游（如 aud=baffle）的 token 不能拿来调本仓。"""
    _mock_jwks(_jwk(_BFF_KEY, "WBTHK01-1"))
    assert await _verifier().verify(_token(_BFF_KEY, audience="baffle")) is None


@respx.mock
async def test_wrong_issuer_rejected() -> None:
    _mock_jwks(_jwk(_BFF_KEY, "WBTHK01-1"))
    assert await _verifier().verify(_token(_BFF_KEY, issuer="someone-else")) is None


@respx.mock
async def test_expired_token_rejected() -> None:
    _mock_jwks(_jwk(_BFF_KEY, "WBTHK01-1"))
    # -600s：越过 leeway=30s
    assert await _verifier().verify(_token(_BFF_KEY, expires_in=-600)) is None


@respx.mock
async def test_token_without_exp_rejected() -> None:
    """无 exp 的 token 等于永不过期——必须靠 require 显式拒掉，而不是被默认跳过。"""
    _mock_jwks(_jwk(_BFF_KEY, "WBTHK01-1"))
    assert await _verifier().verify(_token(_BFF_KEY, drop=("exp",))) is None


@respx.mock
async def test_signature_from_unknown_key_rejected() -> None:
    """kid 对得上、签名却是别的私钥签的 → 拒。"""
    _mock_jwks(_jwk(_BFF_KEY, "WBTHK01-1"))
    assert await _verifier().verify(_token(_ROGUE_KEY, kid="WBTHK01-1")) is None


@respx.mock
async def test_kid_not_in_jwks_rejected() -> None:
    _mock_jwks(_jwk(_BFF_KEY, "WBTHK01-1"))
    assert await _verifier().verify(_token(_BFF_KEY, kid="WBTHK01-99")) is None


@respx.mock
async def test_jwks_unreachable_fails_closed() -> None:
    """JWKS 拉不到 → 拒，不是放行。"""
    respx.get(JWKS_URL).mock(side_effect=httpx.ConnectError("boom"))
    assert await _verifier().verify(_token(_BFF_KEY)) is None


@respx.mock
async def test_jwks_non_2xx_fails_closed() -> None:
    respx.get(JWKS_URL).mock(return_value=httpx.Response(503))
    assert await _verifier().verify(_token(_BFF_KEY)) is None


@respx.mock
async def test_jwks_without_keys_field_fails_closed() -> None:
    """端点在服但返回体没有 keys（BFF 半坏）→ 空缓存 → 拒。"""
    respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json={}))
    assert await _verifier().verify(_token(_BFF_KEY)) is None


@respx.mock
async def test_jwk_without_kid_is_skipped() -> None:
    """JWKS 里没有 kid 的条目无法被定位，直接丢弃而不是当默认键用。"""
    keyless = _jwk(_BFF_KEY, "WBTHK01-1")
    keyless.pop("kid")
    _mock_jwks(keyless)
    assert await _verifier().verify(_token(_BFF_KEY)) is None


@respx.mock
async def test_unreadable_jwk_rejected() -> None:
    """kid 命中但 JWK 本身解不出公钥 → 拒（而不是把异常抛给中间件变 500）。"""
    _mock_jwks({"kid": "WBTHK01-1", "kty": "EC", "crv": "P-256"})
    assert await _verifier().verify(_token(_BFF_KEY)) is None


@respx.mock
async def test_empty_token_rejected() -> None:
    _mock_jwks(_jwk(_BFF_KEY, "WBTHK01-1"))
    assert await _verifier().verify("") is None


@respx.mock
async def test_malformed_token_rejected() -> None:
    _mock_jwks(_jwk(_BFF_KEY, "WBTHK01-1"))
    assert await _verifier().verify("not-a-jwt") is None


@respx.mock
async def test_non_rs256_alg_rejected() -> None:
    """HS256 用公钥当 HMAC 密钥是经典绕过；alg 白名单必须在验签前就把它挡掉。"""
    _mock_jwks(_jwk(_BFF_KEY, "WBTHK01-1"))
    now = int(time.time())
    forged = pyjwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "sub": "x", "exp": now + 300},
        "secret",
        algorithm="HS256",
        headers={"kid": "WBTHK01-1"},
    )
    assert await _verifier().verify(forged) is None


@respx.mock
async def test_token_without_kid_rejected() -> None:
    _mock_jwks(_jwk(_BFF_KEY, "WBTHK01-1"))
    now = int(time.time())
    no_kid = pyjwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "sub": "x", "exp": now + 300},
        _BFF_KEY,
        algorithm="RS256",
    )
    assert await _verifier().verify(no_kid) is None


@respx.mock
async def test_jwks_cached_within_ttl() -> None:
    """TTL 内不回源：一次验签拉一次 JWKS 会把 BFF 打成本仓的同步依赖。"""
    route = _mock_jwks(_jwk(_BFF_KEY, "WBTHK01-1"))
    verifier = _verifier()
    assert await verifier.verify(_token(_BFF_KEY)) is not None
    assert await verifier.verify(_token(_BFF_KEY)) is not None
    assert route.call_count == 1


@respx.mock
async def test_unknown_kid_within_ttl_does_not_refetch() -> None:
    """陌生 kid 不触发回源——否则伪造 token 就是一根免费的打 BFF 的杠杆。"""
    route = _mock_jwks(_jwk(_BFF_KEY, "WBTHK01-1"))
    verifier = _verifier()
    assert await verifier.verify(_token(_BFF_KEY)) is not None
    assert await verifier.verify(_token(_BFF_KEY, kid="ghost")) is None
    assert route.call_count == 1


@respx.mock
async def test_cache_refreshes_after_ttl_expiry() -> None:
    """TTL 过后回源，轮换出来的新 kid 由此接上。"""
    route = respx.get(JWKS_URL).mock(
        side_effect=[
            httpx.Response(200, json={"keys": [_jwk(_BFF_KEY, "WBTHK01-1")]}),
            httpx.Response(200, json={"keys": [_jwk(_BFF_KEY, "WBTHK01-2")]}),
        ]
    )
    verifier = _verifier(cache_ttl_seconds=0.0)
    assert await verifier.verify(_token(_BFF_KEY, kid="WBTHK01-1")) is not None
    assert await verifier.verify(_token(_BFF_KEY, kid="WBTHK01-2")) is not None
    assert route.call_count == 2


@respx.mock
async def test_jwks_fetch_sends_internal_caller_header() -> None:
    """BFF 的 /internal/* 入口要求 X-Caller-Service，缺了会被 400 挡掉。"""
    route = _mock_jwks(_jwk(_BFF_KEY, "WBTHK01-1"))
    await _verifier().verify(_token(_BFF_KEY))
    assert route.calls.last.request.headers["X-Caller-Service"] == "internal"


@respx.mock
async def test_failed_refresh_drops_stale_keys() -> None:
    """回源失败时丢掉旧缓存：已吊销的公钥不能在 BFF 挂掉期间继续验过 token。"""
    respx.get(JWKS_URL).mock(
        side_effect=[
            httpx.Response(200, json={"keys": [_jwk(_BFF_KEY, "WBTHK01-1")]}),
            httpx.ConnectError("bff down"),
        ]
    )
    verifier = _verifier(cache_ttl_seconds=0.0)
    assert await verifier.verify(_token(_BFF_KEY)) is not None
    assert await verifier.verify(_token(_BFF_KEY)) is None


@pytest.mark.parametrize("dropped", ["aud", "iss"])
@respx.mock
async def test_required_claims_must_be_present(dropped: str) -> None:
    _mock_jwks(_jwk(_BFF_KEY, "WBTHK01-1"))
    assert await _verifier().verify(_token(_BFF_KEY, drop=(dropped,))) is None


@respx.mock
async def test_concurrent_first_use_fetches_jwks_once() -> None:
    """冷启动时并发请求只回源一次——否则一次流量尖峰会把 BFF 打成拉取风暴。"""

    async def slow_jwks(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.02)
        return httpx.Response(200, json={"keys": [_jwk(_BFF_KEY, "WBTHK01-1")]})

    route = respx.get(JWKS_URL).mock(side_effect=slow_jwks)
    verifier = _verifier()
    token = _token(_BFF_KEY)
    results = await asyncio.gather(*(verifier.verify(token) for _ in range(4)))
    assert all(claims is not None for claims in results)
    assert route.call_count == 1
