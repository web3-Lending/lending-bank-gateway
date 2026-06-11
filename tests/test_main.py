import pytest
from fastapi import FastAPI

from app.clients.wedap import WedapClient
from app.core.config import get_settings
from app.main import create_app


def test_create_app_returns_fastapi(app: FastAPI) -> None:
    assert isinstance(app, FastAPI)


def test_create_app_initializes_wedap_client() -> None:
    """create_app 必须初始化 app.state.wedap 为 WedapClient 实例。

    防回归：生产首个回调不能因 wedap 未接线而 AttributeError。
    """
    result = create_app()
    assert isinstance(result.state.wedap, WedapClient)


def test_create_app_prod_without_secret_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """GW_ENV=prod 且无 secret → create_app 必须 RuntimeError（fail-fast）。"""
    get_settings.cache_clear()
    monkeypatch.setenv("GW_ENV", "prod")
    monkeypatch.delenv("GW_S2S_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="GW_S2S_SECRET"):
        create_app()
    get_settings.cache_clear()


def test_create_app_prod_with_secret_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """GW_ENV=prod + secret 已设置 → create_app 正常建 app。"""
    get_settings.cache_clear()
    monkeypatch.setenv("GW_ENV", "prod")
    monkeypatch.setenv("GW_S2S_SECRET", "super-secret-token")
    result = create_app()
    assert isinstance(result, FastAPI)
    get_settings.cache_clear()


def test_create_app_local_without_secret_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """GW_ENV=local（默认）无 secret → 允许，不抛异常。"""
    get_settings.cache_clear()
    monkeypatch.setenv("GW_ENV", "local")
    monkeypatch.delenv("GW_S2S_SECRET", raising=False)
    result = create_app()
    assert isinstance(result, FastAPI)
    get_settings.cache_clear()


def test_create_app_test_env_without_secret_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """GW_ENV=test 无 secret → 允许，不抛异常。"""
    get_settings.cache_clear()
    monkeypatch.setenv("GW_ENV", "test")
    monkeypatch.delenv("GW_S2S_SECRET", raising=False)
    result = create_app()
    assert isinstance(result, FastAPI)
    get_settings.cache_clear()
