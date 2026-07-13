import asyncio
import logging
import sys
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


def test_create_app_parses_per_service_tokens_a_m_002() -> None:
    """A-m-002：create_app 解析 GW_S2S_CALLER_TOKENS 启用 per-service 校验（覆盖解析分支）。"""
    from app.core.config import Settings

    app = create_app(
        Settings(env="test", s2s_caller_tokens="svc-a:tok-a , svc-b:tok-b , bad-no-colon, :empty")
    )
    client = TestClient(app)
    # s2s 中间件在 handler/DB 之前执行：错误 token → 401
    r = client.get(
        "/api/v1/bank-funds/status?bizSeqNo=X",
        headers={"X-Caller-Service": "svc-a", "X-S2S-Token": "WRONG"},
    )
    assert r.status_code == 401
    # 未登记 caller（含被忽略的畸形条目）→ 401
    r2 = client.get(
        "/api/v1/bank-funds/status?bizSeqNo=X",
        headers={"X-Caller-Service": "bad-no-colon", "X-S2S-Token": "tok-a"},
    )
    assert r2.status_code == 401


def test_create_app_prod_without_secret_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """GW_ENV=prod 且无 secret → create_app 必须 RuntimeError（fail-fast）。"""
    get_settings.cache_clear()
    monkeypatch.setenv("GW_ENV", "prod")
    monkeypatch.delenv("GW_S2S_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="GW_S2S_SECRET"):
        create_app()
    get_settings.cache_clear()


def test_create_app_prod_with_secret_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """GW_ENV=prod + 两个 secret（S2S + wedap 回调 apikey）已设置 → create_app 正常建 app。"""
    get_settings.cache_clear()
    monkeypatch.setenv("GW_ENV", "prod")
    monkeypatch.setenv("GW_S2S_SECRET", "super-secret-token")
    monkeypatch.setenv("GW_WEDAP_CALLBACK_API_KEY", "super-secret-callback-key")
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


class TestWorkerLogging:
    """GW-WORKER-LOGGING：create_app 必须给 root 装 stdout handler，否则 app.* 的
    INFO/exception 在容器 docker logs 里静默（worker 启停/崩溃/reconcile 全不可见）。"""

    @staticmethod
    def _reset_logging() -> None:
        import app.main as main_mod

        root = logging.getLogger()
        for h in [
            h
            for h in root.handlers
            if isinstance(h, logging.StreamHandler) and getattr(h, "stream", None) is sys.stdout
        ]:
            root.removeHandler(h)
        main_mod._logging_configured = False

    @staticmethod
    def _stdout_handlers() -> list[logging.Handler]:
        return [
            h
            for h in logging.getLogger().handlers
            if isinstance(h, logging.StreamHandler) and getattr(h, "stream", None) is sys.stdout
        ]

    def test_create_app_installs_stdout_handler(self) -> None:
        """create_app 后 root 有指向 stdout 的 StreamHandler，级别 ≤ INFO。"""
        self._reset_logging()
        create_app()
        assert self._stdout_handlers(), "root 应有指向 stdout 的 StreamHandler"
        assert logging.getLogger().level <= logging.INFO

    def test_app_logger_info_propagates_to_root(self, caplog: pytest.LogCaptureFixture) -> None:
        """app.main 的 logger.info 经 propagate 落到 root（此前 root 无 handler → 静默）。"""
        from app.main import logger as app_logger

        self._reset_logging()
        create_app()
        with caplog.at_level(logging.INFO):
            app_logger.info("worker probe line")
        assert any("worker probe line" in r.message for r in caplog.records)

    def test_invalid_log_level_falls_back_to_info(self) -> None:
        """非法 GW_LOG_LEVEL 回退 INFO，不抛异常。"""
        from app.core.config import Settings

        self._reset_logging()
        create_app(Settings(env="test", log_level="NOPE"))
        assert logging.getLogger().level == logging.INFO

    def test_log_level_honored(self) -> None:
        """合法 GW_LOG_LEVEL（DEBUG）生效到 root。"""
        from app.core.config import Settings

        self._reset_logging()
        create_app(Settings(env="test", log_level="debug"))
        assert logging.getLogger().level == logging.DEBUG

    def test_configure_logging_idempotent(self) -> None:
        """重复 create_app 不重复装 handler，避免日志行翻倍。"""
        self._reset_logging()
        create_app()
        create_app()
        assert len(self._stdout_handlers()) == 1

    def test_stdout_handler_emits_utc_snakecase_json(self) -> None:
        """stdout handler 输出结构化 JSON，time 为 aware UTC +00:00、字段 snake_case
        (trace_id)——lending 内部标准(05 §2 + 11)，外部平台格式由 agent 出口转。"""
        import json
        import re

        self._reset_logging()
        create_app()
        handler = self._stdout_handlers()[0]
        record = logging.LogRecord("gw.test", logging.INFO, __file__, 0, "probe", None, None)
        payload = json.loads(handler.format(record))
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+00:00$", payload["time"]), (
            payload["time"]
        )
        assert payload["level"] == "INFO"
        assert payload["logger"] == "gw.test"
        assert payload["msg"] == "probe"
        assert "trace_id" in payload
        assert "traceId" not in payload

    def test_json_formatter_includes_exception(self) -> None:
        """record 带 exc_info 时 JSON 含 exception 栈。"""
        import json

        self._reset_logging()
        create_app()
        handler = self._stdout_handlers()[0]
        try:
            raise ValueError("boom")
        except ValueError:
            import sys as _sys

            rec = logging.LogRecord("gw", logging.ERROR, __file__, 0, "err", None, _sys.exc_info())
        payload = json.loads(handler.format(rec))
        assert "exception" in payload
        assert any("ValueError" in line for line in payload["exception"])

    def test_json_formatter_trace_id_fallback_on_context_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """current_ids 异常时 traceId 回退 trc-none，formatter 不抛。"""
        import json

        import app.core.context as ctx

        self._reset_logging()
        create_app()
        handler = self._stdout_handlers()[0]

        def _boom() -> object:
            raise RuntimeError("ctx unavailable")

        monkeypatch.setattr(ctx, "current_ids", _boom)
        rec = logging.LogRecord("gw", logging.INFO, __file__, 0, "x", None, None)
        payload = json.loads(handler.format(rec))
        assert payload["trace_id"] == "trc-none"


def test_lifespan_wedap_delivery_worker_started(monkeypatch: pytest.MonkeyPatch) -> None:
    """GW_WEDAP_DELIVERY_ENABLED=true → lifespan 额外起 wedap-delivery-dispatcher worker。"""
    get_settings.cache_clear()
    monkeypatch.setenv("GW_WORKERS_ENABLED", "true")
    monkeypatch.setenv("GW_WEDAP_DELIVERY_ENABLED", "true")

    started = {"outbox": False, "recon": False, "order": False, "wedap": False}

    def _stub(key: str):
        async def _fake(*_a: object, **_k: object) -> None:
            started[key] = True
            await asyncio.sleep(9999)

        return _fake

    with (
        patch("app.workers.outbox_dispatcher.run_forever", side_effect=_stub("outbox")),
        patch("app.workers.recon_worker.run_forever", side_effect=_stub("recon")),
        patch("app.workers.order_reconcile_worker.run_forever", side_effect=_stub("order")),
        patch("app.workers.wedap_delivery_dispatcher.run_forever", side_effect=_stub("wedap")),
    ):
        with TestClient(create_app()):
            pass

    assert started["wedap"]  # wedap 投递 worker 已起
    get_settings.cache_clear()


def test_create_app_wires_wedap_import_base_url() -> None:
    """gw-internal 分体 base：import_base_url 配置透传进 WedapClient；空则回落 base_url。"""
    from app.core.config import Settings

    split = Settings(
        env="test",
        wedap_base_url="http://gw-internal:8000/lending-gw",
        wedap_import_base_url="http://gw-internal:8000/external/web2-core",
    )
    app = create_app(split)
    assert app.state.wedap._base == "http://gw-internal:8000/lending-gw"
    assert app.state.wedap._import_base == "http://gw-internal:8000/external/web2-core"

    fallback = create_app(Settings(env="test", wedap_base_url="http://baffle:8021"))
    assert fallback.state.wedap._import_base == "http://baffle:8021"
