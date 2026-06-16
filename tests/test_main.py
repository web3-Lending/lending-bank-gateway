import asyncio
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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


def test_create_app_uses_injected_settings_a_m_002() -> None:
    """A-M-002：create_app 接受注入的 Settings 并落到 app.state.settings，不依赖全局 lru_cache。"""
    from app.core.config import Settings

    injected = Settings(env="test", wedap_base_url="http://injected-wedap:9999")
    app = create_app(injected)
    assert app.state.settings is injected
    # 注入的配置真实生效（wedap client 用注入的 base_url）
    assert app.state.wedap._base == "http://injected-wedap:9999"


def test_create_app_default_settings_from_factory() -> None:
    """不传 settings 时回退到 get_settings() 工厂，app.state.settings 存在。"""
    from app.core.config import Settings

    app = create_app()
    assert isinstance(app.state.settings, Settings)


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


# ── lifespan worker 接线测试（C-1）────────────────────────────────────────────────


def test_lifespan_workers_disabled_no_tasks_started(monkeypatch: pytest.MonkeyPatch) -> None:
    """GW_WORKERS_ENABLED=false → lifespan 不启动任何后台 task。

    用 TestClient with 语境触发 lifespan；patch run_forever 为可观测 stub，
    断言 startup 期间未被调度。
    """
    get_settings.cache_clear()
    monkeypatch.setenv("GW_WORKERS_ENABLED", "false")

    outbox_called = False
    recon_called = False

    async def fake_outbox_forever(*_args: object, **_kwargs: object) -> None:
        nonlocal outbox_called
        outbox_called = True
        await asyncio.sleep(0)

    async def fake_recon_forever(*_args: object, **_kwargs: object) -> None:
        nonlocal recon_called
        recon_called = True
        await asyncio.sleep(0)

    with (
        patch("app.workers.outbox_dispatcher.run_forever", side_effect=fake_outbox_forever),
        patch("app.workers.recon_worker.run_forever", side_effect=fake_recon_forever),
    ):
        with TestClient(create_app()):
            assert not outbox_called
            assert not recon_called

    get_settings.cache_clear()


def test_lifespan_workers_enabled_tasks_started_and_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GW_WORKERS_ENABLED=true → lifespan startup 时两个 task 被调度，shutdown 时被 cancel。

    stub run_forever 在被 cancel 前挂起（await asyncio.sleep 大值），
    断言 startup 后 stub 已被调用（task 已创建）；TestClient __exit__ 触发 shutdown cancel。
    """
    get_settings.cache_clear()
    monkeypatch.setenv("GW_WORKERS_ENABLED", "true")

    outbox_started = False
    recon_started = False

    async def fake_outbox_forever(*_args: object, **_kwargs: object) -> None:
        nonlocal outbox_started
        outbox_started = True
        await asyncio.sleep(9999)  # 挂起直到被 cancel

    async def fake_recon_forever(*_args: object, **_kwargs: object) -> None:
        nonlocal recon_started
        recon_started = True
        await asyncio.sleep(9999)

    with (
        patch("app.workers.outbox_dispatcher.run_forever", side_effect=fake_outbox_forever),
        patch("app.workers.recon_worker.run_forever", side_effect=fake_recon_forever),
    ):
        with TestClient(create_app()):
            # lifespan startup 完成，两个 task 已 create_task（coroutine 已被调度）
            # 给 event loop 一个机会执行已 scheduled 的 tasks
            pass
        # TestClient __exit__ 完成 → shutdown → tasks cancelled → 不抛异常

    # stub 被调用即证明 task 被创建并开始运行
    assert outbox_started
    assert recon_started

    get_settings.cache_clear()


def test_lifespan_workers_use_dedicated_pool_a_m_003(monkeypatch: pytest.MonkeyPatch) -> None:
    """A-M-003：worker 用独立 session_factory（≠ app.state.session_factory），与 API 连接池隔离。"""
    get_settings.cache_clear()
    monkeypatch.setenv("GW_WORKERS_ENABLED", "true")

    captured: dict[str, object] = {}

    async def fake_outbox_forever(*args: object, **_kwargs: object) -> None:
        captured["factory"] = args[0]  # run_forever 第一个位置参数 = session_factory
        await asyncio.sleep(9999)

    async def fake_recon_forever(*_args: object, **_kwargs: object) -> None:
        await asyncio.sleep(9999)

    app = create_app()
    with (
        patch("app.workers.outbox_dispatcher.run_forever", side_effect=fake_outbox_forever),
        patch("app.workers.recon_worker.run_forever", side_effect=fake_recon_forever),
    ):
        with TestClient(app):
            pass

    assert "factory" in captured
    # worker 用专用 factory，与 API 的 app.state.session_factory 不是同一个
    assert captured["factory"] is not app.state.session_factory

    get_settings.cache_clear()
