from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GW_", env_file=".env", extra="ignore")

    @field_validator("s3_endpoint_url", "s2s_secret", mode="before")
    @classmethod
    def _empty_env_as_none(cls, v: object) -> object:
        """空串/纯空白环境变量归一为 None（可空字段语义 = 未配置）。

        compose 的 environment 段无法条件注入，`${VAR:-}` 会把未定义变量写成空串；
        s3_endpoint_url 若拿到 "" 会被 boto3 当字面 endpoint 抛 Invalid endpoint
        （recon-worker 启动崩溃循环）。s2s_secret 本就"空串与 None 等价"，一并归一。
        """
        if isinstance(v, str) and not v.strip():
            return None
        return v

    db_url: str = "sqlite+aiosqlite:///:memory:"
    wedap_base_url: str = "http://localhost:8021"
    wedap_timeout_seconds: float = 10.0
    bank_timezone: str = "Asia/Hong_Kong"
    s3_endpoint_url: str | None = None
    # wedap flow-import 投递（§4.3）：staging（recon 写/gateway 读）+ 导入 bucket（待 wedap 凭证）。
    wedap_staging_bucket: str = "lending-wedap-staging"
    wedap_import_bucket: str = "wedap-flow-import"
    wedap_import_api_key: str = ""  # wedap flow-import notify apikey（FLOW_IMPORT_API_KEYS 之一）
    # flow-import 独立 base（GW_WEDAP_IMPORT_BASE_URL）：银行南向与 import 经 gw-internal
    # 的路由前缀不同（/lending-gw → adapter vs /external/web2-core → web2-core），一个
    # base 罩不住。空 = 回落 wedap_base_url（local/test，import 链路默认关闭）。
    wedap_import_base_url: str = ""
    # wedap→gateway 入站回调 apikey（FU-GW-INBOUND-AUTH-WEDAP-CALLBACK）。wedap 调
    # /api/v1/callbacks/wedap/transactions 与 /api/v1/recon/notify 时带 `apikey` header，
    # 由 S2SMiddleware 在 body 解析前认证（回调不验签，无 body 签名，2026-07-06 决策）。
    # 空=dev 降级放行（仅 local/test）；非 local/test 环境未配则 create_app 启动期 fail-fast
    # （资金网关禁 fail-open），prod/dev-hw 必配。
    wedap_callback_api_key: str = ""
    # 退款分流护栏（FU-GW-REVERSAL-INGESTION · 用户拍板 2026-07-15：全额退款走冲正、
    # refund 仅部分退款）。默认关：wedap 冲正 4.8 未落地前全额仍走 refund（过渡口径），
    # 4.8 落地后置 true——refundAmount == 原归集单金额的请求被 422 导流冲正接口。
    refund_full_amount_guard: bool = False
    wedap_delivery_interval_seconds: float = 5.0
    wedap_delivery_max_attempts: int = 5
    wedap_delivery_enabled: bool = False  # wedap 投递 worker 开关（默认关，e2e/部署显式开）
    # §6.1 五护栏（切流 silent-failure 防护）配置。
    wedap_result_scan_anchor_hour: int = 2
    """wedap BatchScanScheduler 每日运行点的 UTC 小时（GW_WEDAP_RESULT_SCAN_ANCHOR_HOUR）。

    护栏②：受理时 result_deadline_at = 受理后下一个 anchor 时刻 + grace。默认 2 对齐
    web2-core 默认 cron ``0 0 2 * * ?``；wedap 侧改 cron 时同步调整本值。
    """
    wedap_result_grace_minutes: float = 30.0
    """scanner 窗口后的 result 宽限分钟数（GW_WEDAP_RESULT_GRACE_MINUTES，§6.1 定 30min）。"""
    wedap_delivery_pending_max_age_seconds: float = 1800.0
    """任务停留 PENDING/SENDING 超此秒数 → PENDING_STUCK 告警（护栏③，默认 30min）。
    GW_WEDAP_DELIVERY_PENDING_MAX_AGE_SECONDS"""
    wedap_delivery_alert_batch_limit: int = 100
    """每轮护栏告警扫描的行上限（GW_WEDAP_DELIVERY_ALERT_BATCH_LIMIT）。"""
    wedap_presigned_enabled: bool = False
    """预签名投递/读取开关（GW_WEDAP_PRESIGNED_ENABLED · ADR-0001 P4）。

    默认 False = 现有 boto3 直传/直读（需长期 wedap S3 凭证），现网行为不变。
    True = 投递上传先向 web2-core 申请 presigned PUT URL 再 HTTP PUT，结果读取先申请
    presigned GET URL 再 HTTP GET，lending 无需长期 wedap S3 凭证。
    """
    # gateway→recon 投递回执回写地址（§4.3）。
    recon_base_url: str = "http://lending-recon:8040"
    # 回执 HMAC 共享密钥（= recon RECON_S2S_HMAC_SECRET）；空=local insecure 占位签名。
    recon_callback_hmac_secret: str = ""
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
    order_reconcile_batch_limit: int = 100
    """每轮兜底处理的 order 上限（GW_ORDER_RECONCILE_BATCH_LIMIT）。"""
    archive_dir: str = "/srv/archive"
    """对账文件本地归档目录（GW_ARCHIVE_DIR）。"""
    worker_restart_delay_seconds: float = 5.0
    """worker 崩溃后退避重启间隔秒数（GW_WORKER_RESTART_DELAY_SECONDS）。"""


@lru_cache
def get_settings() -> Settings:
    return Settings()
