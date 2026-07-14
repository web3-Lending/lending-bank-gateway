import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    DateTime,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin

# SQLite 的 autoincrement 只支持 INTEGER 类型；MySQL 生产环境需要 BIGINT。
# with_variant 在 SQLite 测试中降级为 Integer，在 MySQL 中保持 BigInteger。
_BIG_PK = BigInteger().with_variant(Integer, "sqlite")


class BankTxnOrder(Base, TenantMixin, TimestampMixin):
    __tablename__ = "bank_txn_order"
    __table_args__ = (
        UniqueConstraint("tenant_id", "biz_seq_no", name="uq_order_tenant_biz"),
        # 历史遗留：原为 bank_txn_leg 复合 FK 提供父键（leg 已按 C5 拆除，索引保留与 DB 一致）
        UniqueConstraint("id", "tenant_id", name="uq_order_id_tenant"),
    )

    id: Mapped[int] = mapped_column(_BIG_PK, primary_key=True, autoincrement=True)
    biz_seq_no: Mapped[str] = mapped_column(String(32), nullable=False)
    business_action: Mapped[str] = mapped_column(String(32), nullable=False)
    biz_type: Mapped[str] = mapped_column(String(8), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(21, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    caller_service: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    request_id: Mapped[str | None] = mapped_column(String(64))
    submitted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    acked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    finalized_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    # 终态来源留痕：SYNC（同步 HTTP 终态）/ CALLBACK（回调收口）/ RECONCILE（兜底 worker）
    # 给 recon 对账「同步 vs 回调谁收口」+ 排查重复 webhook 用
    finalized_via: Mapped[str | None] = mapped_column(String(12))
