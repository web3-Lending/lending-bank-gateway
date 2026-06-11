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
