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
