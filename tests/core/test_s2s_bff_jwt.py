"""S2S 中间件接受 BFF svc JWT 的规格（collab brj8tl7t90a7i5n169g69u23）。

背景：调用方经 BFF `/internal/proxy/bank-gateway/...` 打进来时带的是
`Authorization: Bearer <svc JWT>`，**没有** X-S2S-Token——BFF 的出站透传白名单里
没有这个头，调用方也补不了。在本通路打通前，9000 经 7018 调 8022 的每一个业务
端点都是 401 GW_401_S2S。
"""

import json
import time
from typing import Any

import httpx
import jwt as pyjwt
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm

from app.core.context import IdentifierMiddleware
from app.core.s2s import S2SMiddleware
from app.core.svc_jwt import BffSvcJwtVerifier

JWKS_URL = "http://bff:7018/internal/svc-jwks.json"
_BFF_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_KID = "WBTHK01-1"


def _jwks_payload() -> dict[str, Any]:
    jwk: dict[str, Any] = json.loads(RSAAlgorithm.to_jwk(_BFF_KEY.public_key()))
    jwk["kid"] = _KID
    return {"keys": [jwk]}


def _bearer(audience: str = "bank-gateway", caller_service: str = "lifecycle") -> str:
    now = int(time.time())
    return pyjwt.encode(
        {
            "iss": "lending-console-bff",
            "aud": audience,
            "sub": "bff-service",
            "tenant_id": "WBTHK01",
            "caller_service": caller_service,
            "jti": "jti-1",
            "iat": now,
            "exp": now + 300,
        },
        _BFF_KEY,
        algorithm="RS256",
        headers={"kid": _KID},
    )


def _verifier() -> BffSvcJwtVerifier:
    return BffSvcJwtVerifier(
        jwks_url=JWKS_URL,
        audience="bank-gateway",
        issuer="lending-console-bff",
        cache_ttl_seconds=300.0,
        leeway_seconds=30.0,
        timeout_seconds=5.0,
    )


def _app(
    *,
    secret: str | None = "sec",  # noqa: S107  # 测试用
    with_verifier: bool = True,
    allowed_callers: set[str] | None = None,
    caller_tokens: dict[str, str] | None = None,
) -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        S2SMiddleware,
        secret=secret,
        exempt_paths={"/healthz"},
        allowed_callers=allowed_callers,
        caller_tokens=caller_tokens,
        svc_jwt_verifier=_verifier() if with_verifier else None,
    )
    app.add_middleware(IdentifierMiddleware)

    @app.post("/api/v1/x")
    async def x() -> dict:  # type: ignore[misc]
        return {"ok": True}

    @app.get("/api/v1/probe")
    async def probe(request: Request) -> dict:  # type: ignore[misc]
        return {"token_bound": getattr(request.state, "s2s_token_bound", None)}

    return app


def _mock_jwks() -> respx.Route:
    return respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json=_jwks_payload()))


@respx.mock
def test_bff_svc_jwt_without_s2s_token_passes() -> None:
    """本单的核心：只有 Authorization + caller 头，也应进业务层。"""
    _mock_jwks()
    r = TestClient(_app()).post(
        "/api/v1/x",
        headers={"X-Caller-Service": "lifecycle", "Authorization": f"Bearer {_bearer()}"},
    )
    assert r.status_code == 200


@respx.mock
def test_bff_svc_jwt_for_another_audience_rejected() -> None:
    """BFF 发给 baffle 的 token 不能拿来调本仓。"""
    _mock_jwks()
    r = TestClient(_app()).post(
        "/api/v1/x",
        headers={
            "X-Caller-Service": "lifecycle",
            "Authorization": f"Bearer {_bearer(audience='baffle')}",
        },
    )
    assert r.status_code == 401
    assert r.json()["error"]["message"] == "bad bff svc token"
    assert r.headers["www-authenticate"].startswith("S2S ")
    assert "Bearer" in r.headers["www-authenticate"]


@respx.mock
def test_bad_bearer_does_not_fall_back_to_shared_secret() -> None:
    """验签失败就是失败，不许回落去撞共享 secret。"""
    _mock_jwks()
    r = TestClient(_app()).post(
        "/api/v1/x",
        headers={"X-Caller-Service": "lifecycle", "Authorization": "Bearer garbage"},
    )
    assert r.status_code == 401


@respx.mock
def test_jwks_unreachable_fails_closed() -> None:
    respx.get(JWKS_URL).mock(side_effect=httpx.ConnectError("bff down"))
    r = TestClient(_app()).post(
        "/api/v1/x",
        headers={"X-Caller-Service": "lifecycle", "Authorization": f"Bearer {_bearer()}"},
    )
    assert r.status_code == 401


@respx.mock
def test_missing_caller_header_still_rejected_with_valid_jwt() -> None:
    """caller 头是 S2S 面的恒定要求，换了凭证也不豁免。"""
    _mock_jwks()
    r = TestClient(_app()).post(
        "/api/v1/x", headers={"Authorization": f"Bearer {_bearer()}"}
    )
    assert r.status_code == 401
    assert r.json()["error"]["message"] == "missing X-Caller-Service"


@respx.mock
def test_caller_whitelist_applies_on_jwt_path() -> None:
    """本仓自己的准入名单不因为凭证换成 BFF 签的就被绕过。"""
    _mock_jwks()
    r = TestClient(_app(allowed_callers={"liquidation"})).post(
        "/api/v1/x",
        headers={"X-Caller-Service": "lifecycle", "Authorization": f"Bearer {_bearer()}"},
    )
    assert r.status_code == 401
    assert r.json()["error"]["message"] == "unknown caller"


@respx.mock
def test_whitelisted_caller_on_jwt_path_passes() -> None:
    _mock_jwks()
    r = TestClient(_app(allowed_callers={"lifecycle"})).post(
        "/api/v1/x",
        headers={"X-Caller-Service": "lifecycle", "Authorization": f"Bearer {_bearer()}"},
    )
    assert r.status_code == 200


@respx.mock
def test_s2s_token_takes_precedence_over_bearer() -> None:
    """带了 X-S2S-Token 就按直连规则判：token 对 → 过，Bearer 是什么都不影响。"""
    _mock_jwks()
    r = TestClient(_app()).post(
        "/api/v1/x",
        headers={
            "X-Caller-Service": "lifecycle",
            "X-S2S-Token": "sec",
            "Authorization": "Bearer garbage",
        },
    )
    assert r.status_code == 200


@respx.mock
def test_wrong_s2s_token_is_not_rescued_by_valid_bearer() -> None:
    """反向：直连 token 写错时，不给它"再拿 JWT 试一次"的旁路。"""
    _mock_jwks()
    r = TestClient(_app()).post(
        "/api/v1/x",
        headers={
            "X-Caller-Service": "lifecycle",
            "X-S2S-Token": "wrong",
            "Authorization": f"Bearer {_bearer()}",
        },
    )
    assert r.status_code == 401
    assert r.json()["error"]["message"] == "bad s2s token"


@respx.mock
def test_non_bearer_authorization_falls_through_to_shared_secret() -> None:
    """`Authorization: Basic ...` 不是本仓的凭证形态，按"没出示 Bearer"处理。"""
    _mock_jwks()
    r = TestClient(_app()).post(
        "/api/v1/x",
        headers={"X-Caller-Service": "lifecycle", "Authorization": "Basic zzz"},
    )
    assert r.status_code == 401
    assert r.json()["error"]["message"] == "bad s2s token"


@respx.mock
def test_verifier_absent_means_bearer_is_ignored() -> None:
    """未配 GW_BFF_BASE_URL 的环境：Bearer 一律不作数，行为与改动前逐字节一致。"""
    _mock_jwks()
    r = TestClient(_app(with_verifier=False)).post(
        "/api/v1/x",
        headers={"X-Caller-Service": "lifecycle", "Authorization": f"Bearer {_bearer()}"},
    )
    assert r.status_code == 401
    assert r.json()["error"]["message"] == "bad s2s token"


@respx.mock
def test_jwt_path_is_not_token_bound() -> None:
    """svc JWT 不解锁 admin 配置面——那面只认本仓自己发的专属 token。"""
    _mock_jwks()
    r = TestClient(_app()).get(
        "/api/v1/probe",
        headers={"X-Caller-Service": "lifecycle", "Authorization": f"Bearer {_bearer()}"},
    )
    assert r.status_code == 200
    assert r.json()["token_bound"] is False


@respx.mock
def test_caller_with_own_token_still_must_use_it() -> None:
    """在专属 token 表里的 caller 不能改用 Bearer 绕开自己的 token。"""
    _mock_jwks()
    client = TestClient(_app(caller_tokens={"lifecycle": "tok-life"}))
    r = client.post(
        "/api/v1/x",
        headers={"X-Caller-Service": "lifecycle", "X-S2S-Token": "WRONG"},
    )
    assert r.status_code == 401
