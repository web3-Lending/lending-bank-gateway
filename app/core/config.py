from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GW_", env_file=".env", extra="ignore")

    db_url: str = "sqlite+aiosqlite:///:memory:"
    wedap_base_url: str = "http://localhost:8021"
    wedap_timeout_seconds: float = 10.0
    bank_timezone: str = "Asia/Hong_Kong"
    s3_endpoint_url: str | None = None
    callback_target_lifecycle_url: str = (
        "http://lending-lifecycel:9000/api/v1/bank/transaction-callback"
    )
    s2s_secret: str | None = None
    outbox_max_attempts: int = 8


@lru_cache
def get_settings() -> Settings:
    return Settings()
