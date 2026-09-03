import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings


def test_get_settings_returns_cached_instance() -> None:
    get_settings.cache_clear()
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2
    get_settings.cache_clear()


def test_settings_defaults_and_env_override(monkeypatch) -> None:
    monkeypatch.setenv("GW_DB_URL", "mysql+asyncmy://u:p@h:3306/lending_bank_gateway")
    monkeypatch.setenv("GW_WEDAP_BASE_URL", "http://wedap-dev:8080")
    s = Settings()
    assert s.db_url.endswith("/lending_bank_gateway")
    assert s.wedap_base_url == "http://wedap-dev:8080"
    assert s.wedap_timeout_seconds == 10.0
    assert s.bank_timezone == "Asia/Hong_Kong"


def test_wedap_result_deadline_config_defaults() -> None:
    # 护栏② deadline 对齐 wedap 看门狗：默认 24h 窗口 + 15min 缓冲（wedap v2 §3.1）。
    s = Settings()
    assert s.wedap_result_watchdog_hours == 24
    assert s.wedap_result_buffer_minutes == 15.0


def test_wedap_result_watchdog_hours_rejects_below_24() -> None:
    # ge=24 守不变量：gateway 截止不得早于 wedap deadline（受理 + 24h），否则 wedap 未判死就误报。
    with pytest.raises(ValidationError):
        Settings(wedap_result_watchdog_hours=12)


def test_wedap_result_buffer_minutes_rejects_negative_inf_nan() -> None:
    # ge=0 禁负缓冲（负值把截止提前）；allow_inf_nan=False 禁 inf/nan（防 timedelta 溢出）。
    with pytest.raises(ValidationError):
        Settings(wedap_result_buffer_minutes=-1)
    with pytest.raises(ValidationError):
        Settings(wedap_result_buffer_minutes=float("inf"))
    with pytest.raises(ValidationError):
        Settings(wedap_result_buffer_minutes=float("nan"))


def test_svc_jwt_leeway_rejects_inf_nan_and_out_of_range() -> None:
    """inf/nan 的 leeway 会让 PyJWT 的 exp 校验恒过——等于悄悄关掉 token 时效。

    实测：`jwt.decode(..., leeway=float('inf'))` 对一个 100000 秒前就过期的 token
    照样解出 claims，且不报任何错。启动期拒掉，别让它变成一条只能靠翻日志发现的
    生产事故。上界 300s 与 BFF 的 SERVICE_JWT_TTL_SEC 同量级。
    """
    for bad in (float("inf"), float("nan"), -1, 301):
        with pytest.raises(ValidationError):
            Settings(svc_jwt_leeway_seconds=bad)
    assert Settings(svc_jwt_leeway_seconds=0).svc_jwt_leeway_seconds == 0
    assert Settings(svc_jwt_leeway_seconds=300).svc_jwt_leeway_seconds == 300


def test_svc_jwks_cache_ttl_rejects_inf_nan_and_out_of_range() -> None:
    """缓存越久，BFF 轮换/吊销公钥后本仓仍用旧公钥验过 token 的窗口越长。"""
    for bad in (float("inf"), float("nan"), -1, 3601):
        with pytest.raises(ValidationError):
            Settings(svc_jwks_cache_ttl_seconds=bad)


def test_svc_jwks_timeout_rejects_zero_inf_nan_and_out_of_range() -> None:
    """0 秒超时是 fail-closed 的退化形态（每次必失败），不是配置。"""
    for bad in (0, float("inf"), float("nan"), -1, 31):
        with pytest.raises(ValidationError):
            Settings(svc_jwks_timeout_seconds=bad)
