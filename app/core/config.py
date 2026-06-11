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
    s2s_callers: str = ""
    """逗号分隔的 S2S 调用方白名单（GW_S2S_CALLERS）。空字符串=不启用白名单校验。

    示例：GW_S2S_CALLERS=lending-lifecycel,lending-risk

    注意：v1 基于共享 token，无法密码学绑定 caller；白名单为兜底归因加固。
    v2 规划 per-service token/签名绑定，届时可移除本字段。
    """
    env: str = "local"
    outbox_max_attempts: int = 8
    db_pool_size: int = 5
    db_max_overflow: int = 10


@lru_cache
def get_settings() -> Settings:
    return Settings()
