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
    workers_enabled: bool = True
    """是否在进程内启动后台 worker（GW_WORKERS_ENABLED）。
    设为 False 可在多副本中禁用 worker，或在单元测试中关闭后台任务。
    """
    outbox_interval_seconds: float = 5.0
    """outbox dispatcher 每轮投递间隔秒数（GW_OUTBOX_INTERVAL_SECONDS）。"""
    recon_interval_seconds: float = 30.0
    """recon worker 每轮摄取间隔秒数（GW_RECON_INTERVAL_SECONDS）。"""
    archive_dir: str = "/srv/archive"
    """对账文件本地归档目录（GW_ARCHIVE_DIR）。"""
    worker_restart_delay_seconds: float = 5.0
    """worker 崩溃后退避重启间隔秒数（GW_WORKER_RESTART_DELAY_SECONDS）。"""


@lru_cache
def get_settings() -> Settings:
    return Settings()
