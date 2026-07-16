"""平台账户白名单（account guard 的 enforcement 依据，非账户主数据）。

守门人语义：gateway 是资金唯一出口，collect/distribute 的平台账户
（payload 顶层 ``bankAccountNo``）必须命中本表 active 行且 business_scope
∈ allowed_scopes，否则按 GW_ACCOUNT_GUARD_MODE 拒绝（enforce）或记录（observe）。
本表**不是**账户主数据——不存余额 / 开户信息 / 户主，只登记
「哪些平台账户允许走 gateway + 允许哪些 scope」；数据由资金运营提供，
初始 seed 走 alembic data migration，日常增改走 admin 端点。
"""

from sqlalchemy import String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin
from app.models.txn import _BIG_PK


class PlatformBankAccount(Base, TenantMixin, TimestampMixin):
    __tablename__ = "platform_bank_account"
    __table_args__ = (
        UniqueConstraint("tenant_id", "account_no", name="uq_platform_account_tenant_acct"),
    )

    id: Mapped[int] = mapped_column(_BIG_PK, primary_key=True, autoincrement=True)
    account_no: Mapped[str] = mapped_column(String(64), nullable=False)
    # 账户用途标签（人读 + 业务分类）；enforcement 只看 allowed_scopes。
    # 当前集合 escrow|settlement|fee|payout|collateral，不做 DB 约束——新用途直接插新值。
    purpose: Mapped[str] = mapped_column(String(24), nullable=False)
    # csv，如 "bank_collect,bank_distribute"；与 _submit 的 business_scope 同一词表。
    allowed_scopes: Mapped[str] = mapped_column(String(255), nullable=False)
    # 可选币种限定（D4：一期启用）——NULL = 不限币种。
    currency: Mapped[str | None] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    note: Mapped[str | None] = mapped_column(String(255))
