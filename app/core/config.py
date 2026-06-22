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
    s2s_caller_tokens: str = ""
    """per-service S2S token（GW_S2S_CALLER_TOKENS），格式 `caller1:token1,caller2:token2`。

    配置后优先于共享 s2s_secret：按调用方专属 token 校验，把 caller 与 token 密码学绑定
    （A-m-002）——单个 caller 的 token 泄露只能冒充它自己，不能伪造他人 caller。
    空串=不启用，回退共享 secret 模式。mTLS/签名绑定为后续增强。
    """
    s2s_callers: str = ""
    """逗号分隔的 S2S 调用方白名单（GW_S2S_CALLERS）。空字符串=不启用白名单校验。

    示例：GW_S2S_CALLERS=lending-lifecycel,lending-risk

    注意：v1 基于共享 token，无法密码学绑定 caller；白名单为兜底归因加固。
    v2 规划 per-service token/签名绑定，届时可移除本字段。
    """
    env: str = "local"
    log_level: str = "INFO"
    """root logger 级别（GW_LOG_LEVEL）。app.* 的 logger propagate 到 root，
    create_app 时据此给 root 装 stdout handler，使 worker 启停 / 崩溃 / reconcile
    日志在容器 `docker logs` 可见（此前 root 无 handler → INFO 全静默）。
    取值不区分大小写：DEBUG/INFO/WARNING/ERROR；非法值回退 INFO。
    """
    outbox_max_attempts: int = 8
    db_pool_size: int = 5
    db_max_overflow: int = 10
    # worker 专用连接池（A-M-003）：与 API 隔离，避免 worker 慢外呼/长事务争抢在线请求连接
    # 3 worker（outbox / recon-ingest / order-reconcile）共享本池，pool_size 取 4 留余量
    worker_db_pool_size: int = 4
    worker_db_max_overflow: int = 5
    workers_enabled: bool = True
    """是否在进程内启动后台 worker（GW_WORKERS_ENABLED）。
    设为 False 可在多副本中禁用 worker，或在单元测试中关闭后台任务。
    """
    outbox_interval_seconds: float = 5.0
    """outbox dispatcher 每轮投递间隔秒数（GW_OUTBOX_INTERVAL_SECONDS）。"""
    recon_interval_seconds: float = 30.0
    """recon worker 每轮摄取间隔秒数（GW_RECON_INTERVAL_SECONDS）。"""
    order_reconcile_interval_seconds: float = 60.0
    """order-reconcile worker 每轮扫描间隔秒数（GW_ORDER_RECONCILE_INTERVAL_SECONDS）。"""
    order_reconcile_stale_after_seconds: float = 30.0
    """order 未达终态多久视为待兜底（GW_ORDER_RECONCILE_STALE_AFTER_SECONDS）。"""
    order_reconcile_max_age_seconds: float = 604800.0
    """只兜底此窗口内 order，避免无限扫古单（默认 7d）。GW_ORDER_RECONCILE_MAX_AGE_SECONDS"""
    order_reconcile_leg_backfill_seconds: float = 3600.0
    """终态无 leg 的 leg 补拉窗（按 finalized_at，默认 1h）：超此窗放弃补拉，避免 CLT/无 composite
    明细的终态单每轮热重试打爆 wedap。超窗 = 明细永缺（CLT residual risk）。
    GW_ORDER_RECONCILE_LEG_BACKFILL_SECONDS"""
    order_reconcile_batch_limit: int = 100
    """每轮兜底处理的 order 上限（GW_ORDER_RECONCILE_BATCH_LIMIT）。"""
    archive_dir: str = "/srv/archive"
    """对账文件本地归档目录（GW_ARCHIVE_DIR）。"""
    worker_restart_delay_seconds: float = 5.0
    """worker 崩溃后退避重启间隔秒数（GW_WORKER_RESTART_DELAY_SECONDS）。"""


@lru_cache
def get_settings() -> Settings:
    return Settings()
