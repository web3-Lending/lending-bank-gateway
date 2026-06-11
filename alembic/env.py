"""Alembic 迁移环境配置。

URL 注入逻辑：
- 从 GW_DB_URL 环境变量读取（通过 app.core.config.Settings）
- async 驱动替换为 sync 驱动（alembic 本身是同步运行时）：
    mysql+asyncmy → mysql+pymysql
    sqlite+aiosqlite → sqlite
"""

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# Alembic Config 对象，提供对 .ini 文件中值的访问
config = context.config

# 配置日志
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 导入所有模型以确保 Base.metadata 注册完整
import app.models.idempotency  # noqa: F401, E402
import app.models.txn  # noqa: F401, E402
from app.models.base import Base  # noqa: E402

target_metadata = Base.metadata


def _to_sync_url(async_url: str) -> str:
    """将 async 驱动替换为 alembic 所需的 sync 驱动。"""
    return (
        async_url.replace("mysql+asyncmy://", "mysql+pymysql://")
        .replace("sqlite+aiosqlite:///", "sqlite:///")
        .replace("sqlite+aiosqlite://", "sqlite://")
    )


def _get_url() -> str:
    from app.core.config import Settings

    return _to_sync_url(Settings().db_url)


def run_migrations_offline() -> None:
    """离线模式：不建立实际连接，直接输出 SQL。"""
    url = _get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：建立真实数据库连接并执行迁移。

    支持通过 config.attributes["connection"] 注入已有连接（测试用途），
    避免重新建连接带来的认证限制。
    """
    # 测试注入路径：直接使用已有连接，跳过 URL 解析
    injected = config.attributes.get("connection", None)
    if injected is not None:
        context.configure(
            connection=injected,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    cfg = config.get_section(config.config_ini_section, {})
    cfg["sqlalchemy.url"] = _get_url()

    connectable = engine_from_config(
        cfg,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
