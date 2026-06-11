import datetime as dt
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin

# SQLite autoincrement 只支持 INTEGER；MySQL 生产环境需要 BIGINT。
# with_variant 在 SQLite 测试中降级为 Integer，在 MySQL 中保持 BigInteger。
_BIG_PK = BigInteger().with_variant(Integer, "sqlite")


class CallbackInbox(Base, TenantMixin, TimestampMixin):
    """银行/外部系统回调入站记录。

    三元组 (tenant_id, source, request_id) 唯一保证幂等：
    同一来源的同一请求只落一条记录。
    """

    __tablename__ = "callback_inbox"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source", "request_id", name="uq_inbox_tenant_src_req"),
    )

    id: Mapped[int] = mapped_column(_BIG_PK, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)  # WEDAP_TXN / WEDAP_RECON
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # 只写不改：直接修改 dict 不触发 dirty tracking
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="RECEIVED")
    error: Mapped[str | None] = mapped_column(Text)


class CallbackOutbox(Base, TenantMixin, TimestampMixin):
    """对下游服务的事件推送出站记录（dispatcher 扫 status=PENDING 发送）。

    status+next_retry_at 复合索引：dispatcher 按 (status, next_retry_at) 扫表，
    复合索引比单列索引更高效（覆盖两列过滤条件）。
    """

    __tablename__ = "callback_outbox"
    __table_args__ = (Index("ix_callback_outbox_status_retry", "status", "next_retry_at"),)

    id: Mapped[int] = mapped_column(_BIG_PK, primary_key=True, autoincrement=True)
    target: Mapped[str] = mapped_column(String(32), nullable=False)  # lifecycle / customers
    # 只写不改：直接修改 dict 不触发 dirty tracking
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
