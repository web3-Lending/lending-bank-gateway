import datetime as dt
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin

# SQLite 的 autoincrement 只支持 INTEGER 类型；MySQL 生产环境需要 BIGINT。
# with_variant 在 SQLite 测试中降级为 Integer，在 MySQL 中保持 BigInteger。
_BIG_PK = BigInteger().with_variant(Integer, "sqlite")


class BankTxnOrder(Base, TenantMixin, TimestampMixin):
    __tablename__ = "bank_txn_order"
    __table_args__ = (UniqueConstraint("tenant_id", "biz_seq_no", name="uq_order_tenant_biz"),)

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


class BankTxnLeg(Base, TenantMixin, TimestampMixin):
    __tablename__ = "bank_txn_leg"
    __table_args__ = (
        UniqueConstraint("tenant_id", "external_system", "external_ref", name="uq_leg_tenant_ext"),
        UniqueConstraint("tenant_id", "biz_seq_no", "step_seq", name="uq_leg_tenant_biz_step"),
    )

    id: Mapped[int] = mapped_column(_BIG_PK, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("bank_txn_order.id"),
        nullable=False,
    )
    biz_seq_no: Mapped[str] = mapped_column(String(32), nullable=False)
    external_system: Mapped[str] = mapped_column(String(16), nullable=False, default="WEDAP_BANK")
    external_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    step_type: Mapped[str] = mapped_column(String(40), nullable=False)
    step_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(21, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    payer_account: Mapped[str | None] = mapped_column(String(64))
    payee_account: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    txn_date: Mapped[str | None] = mapped_column(String(8))  # BANK_TIMEZONE YYYYMMDD 透传不重算
    posted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
