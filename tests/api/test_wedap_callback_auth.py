"""wedap→gateway 入站回调端点的 apikey 认证守卫测试（FU-GW-INBOUND-AUTH-WEDAP-CALLBACK）。

背景：`/api/v1/callbacks/wedap/transactions`（交易终态回调）与 `/api/v1/recon/notify`
（三方对账结果通知）原在 S2SMiddleware 的 S2S-token 校验下（外部 wedap 无 lending S2S
token 直调会 401）。本改动把这两个 path 改为 S2SMiddleware 的 **callback apikey** 分支：
在 body 解析前、middleware 层用 `apikey` header 认证（最小权限：wedap 只能打这两个回调，
打不了银行 API）。回调不验签（无 body 签名，2026-07-06 决策）。apikey 空=dev 降级放行
（仅 local/test）；非 local/test 未配则 create_app 启动期 fail-fast。
"""

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app
from app.models.base import Base

_CB_PATH = "/api/v1/callbacks/wedap/transactions"
_CB_HEADERS = {"X-Tenant-Id": "WBTHK01", "X-Request-Id": "cb-auth-001"}
_CB_BODY: dict[str, Any] = {"bizSeqNo": "BSQ-20260706-0001", "type": "LOAN_REPAYMENT"}

_RN_PATH = "/api/v1/recon/notify"
_RN_HEADERS = {"X-Tenant-Id": "WBTHK01", "X-Request-Id": "recon-result-RECON-WBTHK01-20260706-v1"}
_RN_BODY: dict[str, Any] = {
    "reconDate": "20260706",
    "tenantId": "WBTHK01",
    "s3Bucket": "wedap-recon-bucket",
    "files": [
        {
            "fileName": "r.xlsx",
            "s3Key": "WBTHK01/20260706/r.xlsx",
            "md5": "A1B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4",
            "totalCount": 0,
        }
    ],
}

_CASES = [(_CB_PATH, _CB_HEADERS, _CB_BODY), (_RN_PATH, _RN_HEADERS, _RN_BODY)]


async def _create_tables(engine: Any) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _client(monkeypatch: pytest.MonkeyPatch, *, api_key: str | None) -> TestClient:
    """设置好 env 后再建 app/client，确保 middleware 读到本用例的 apikey 配置。"""
    if api_key is None:
        monkeypatch.delenv("GW_WEDAP_CALLBACK_API_KEY", raising=False)
    else:
        monkeypatch.setenv("GW_WEDAP_CALLBACK_API_KEY", api_key)
    get_settings.cache_clear()
    app = create_app()
    asyncio.run(_create_tables(app.state.engine))
    return TestClient(app)


@pytest.mark.parametrize("path,headers,body", _CASES)
def test_missing_apikey_rejected_when_configured(
    monkeypatch: pytest.MonkeyPatch, path: str, headers: dict[str, str], body: dict[str, Any]
) -> None:
    client = _client(monkeypatch, api_key="s3cr3t-wedap")
    resp = client.post(path, json=body, headers=headers)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "GW_401_CALLBACK"


@pytest.mark.parametrize("path,headers,body", _CASES)
def test_wrong_apikey_rejected_when_configured(
    monkeypatch: pytest.MonkeyPatch, path: str, headers: dict[str, str], body: dict[str, Any]
) -> None:
    client = _client(monkeypatch, api_key="s3cr3t-wedap")
    resp = client.post(path, json=body, headers={**headers, "apikey": "wrong"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "GW_401_CALLBACK"


@pytest.mark.parametrize("path,headers,body", _CASES)
def test_correct_apikey_reaches_handler_success(
    monkeypatch: pytest.MonkeyPatch, path: str, headers: dict[str, str], body: dict[str, Any]
) -> None:
    """正确 apikey → 认证放行 → 进 handler 且业务成功（200 success 信封），非仅"不 401"。"""
    client = _client(monkeypatch, api_key="s3cr3t-wedap")
    resp = client.post(path, json=body, headers={**headers, "apikey": "s3cr3t-wedap"})
    assert resp.status_code == 200
    assert resp.json()["success"] is True


@pytest.mark.parametrize("path,headers,body", _CASES)
def test_empty_config_dev_degrade_reaches_handler(
    monkeypatch: pytest.MonkeyPatch, path: str, headers: dict[str, str], body: dict[str, Any]
) -> None:
    """apikey 未配（默认空，env=local）= dev 降级放行 → 进 handler 成功，现网行为不因改动破坏。"""
    client = _client(monkeypatch, api_key=None)
    resp = client.post(path, json=body, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["success"] is True


def test_prod_env_missing_apikey_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """非 local/test 环境未配 apikey → create_app 启动期 fail-fast（资金网关禁 fail-open）。"""
    monkeypatch.setenv("GW_ENV", "dev")
    monkeypatch.setenv("GW_S2S_SECRET", "s2s-set-so-only-callback-check-triggers")
    monkeypatch.delenv("GW_WEDAP_CALLBACK_API_KEY", raising=False)
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="GW_WEDAP_CALLBACK_API_KEY"):
        create_app()


def test_banking_api_still_requires_s2s(monkeypatch: pytest.MonkeyPatch) -> None:
    """未过度豁免：非回调的银行 API 缺 X-Caller-Service 仍被 S2S 拦 401。"""
    client = _client(monkeypatch, api_key="s3cr3t-wedap")
    # 银行放款端点，仅带 apikey、不带 X-Caller-Service：apikey 对 S2S 无效 → 401 GW_401_S2S
    resp = client.post(
        "/api/v1/loans/p2p-disbursements",
        json={"bizSeqNo": "X"},
        headers={"X-Tenant-Id": "WBTHK01", "apikey": "s3cr3t-wedap"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "GW_401_S2S"


def test_adjacent_path_not_treated_as_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    """callback 分支是精确 path 匹配：相邻/前缀 path 不被当回调放行，仍走 S2S。"""
    client = _client(monkeypatch, api_key="s3cr3t-wedap")
    resp = client.post(
        "/api/v1/recon/notify-extra",
        json={},
        headers={"X-Tenant-Id": "WBTHK01"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "GW_401_S2S"


@pytest.mark.parametrize("path,headers,body", _CASES)
def test_callback_no_caller_service_allowed(
    monkeypatch: pytest.MonkeyPatch, path: str, headers: dict[str, str], body: dict[str, Any]
) -> None:
    """回调走 apikey 分支、不走 S2S caller 校验：不带 X-Caller-Service 也不因缺 caller 而 401。"""
    client = _client(monkeypatch, api_key="s3cr3t-wedap")
    resp = client.post(path, json=body, headers={**headers, "apikey": "s3cr3t-wedap"})
    assert resp.status_code == 200
