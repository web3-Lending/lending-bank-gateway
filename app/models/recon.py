"""app/models/recon.py

对账结果四张表：
- ReconResultTask     — 对账任务主表（含两个唯一约束）
- ReconResultDiff     — 差异明细
- ReconResultSourceWedap — Wedap 侧对账源数据
- ReconResultSourceBank  — 银行侧对账源数据
"""

from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Index,
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
_BIG_FK = BigInteger().with_variant(Integer, "sqlite")


class ReconResultTask(Base, TenantMixin, TimestampMixin):
    """对账任务主表。

    唯一约束：
    - uq_recon_task_req：(tenant_id, request_id) — 同一租户同一请求只提交一次
    - uq_recon_task_ver：(tenant_id, task_no, version) — 同一对账单号版本唯一
    """

    __tablename__ = "recon_result_task"
    __table_args__ = (
        UniqueConstraint("tenant_id", "request_id", name="uq_recon_task_req"),
        UniqueConstraint("tenant_id", "task_no", "version", name="uq_recon_task_ver"),
    )

    id: Mapped[int] = mapped_column(_BIG_PK, primary_key=True, autoincrement=True)
    task_no: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    recon_date: Mapped[str] = mapped_column(String(8), nullable=False)
    s3_bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    s3_key: Mapped[str] = mapped_column(String(256), nullable=False)
    file_md5: Mapped[str] = mapped_column(String(32), nullable=False)
    diff_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # status 枚举值：NOTIFIED / DOWNLOADED / PARSED / FAILED / SUPERSEDED
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    parser_version: Mapped[str | None] = mapped_column(String(32))
    schema_version: Mapped[str | None] = mapped_column(String(32))
    column_check: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    archive_path: Mapped[str | None] = mapped_column(String(256))


class ReconResultDiff(Base, TenantMixin, TimestampMixin):
    """差异明细表。"""

    __tablename__ = "recon_result_diff"
    __table_args__ = (
        Index("ix_recon_diff_task_id", "task_id"),
        Index("ix_recon_diff_wedap_biz_seq_no", "wedap_biz_seq_no"),
        Index("ix_recon_diff_bank_seq_no", "bank_seq_no"),
        # ix_recon_result_diff_tenant_id 由 TenantMixin(index=True) 自动生成，无需在此重复声明
    )

    id: Mapped[int] = mapped_column(_BIG_PK, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(_BIG_FK, nullable=False)
    diff_type: Mapped[str] = mapped_column(String(8), nullable=False)
    wedap_biz_seq_no: Mapped[str | None] = mapped_column(String(32))
    bank_seq_no: Mapped[str | None] = mapped_column(String(64))
    wedap_amount: Mapped[Decimal | None] = mapped_column(Numeric(21, 4))
    bank_amount: Mapped[Decimal | None] = mapped_column(Numeric(21, 4))
    diff_amount: Mapped[Decimal | None] = mapped_column(Numeric(21, 4))
    wedap_status: Mapped[str | None] = mapped_column(String(16))
    bank_status: Mapped[str | None] = mapped_column(String(16))


class ReconResultSourceWedap(Base, TenantMixin, TimestampMixin):
    """Wedap 侧对账源数据表。"""

    __tablename__ = "recon_result_source_wedap"
    __table_args__ = (
        Index("ix_recon_src_wedap_task_id", "task_id"),
        Index("ix_recon_src_wedap_biz_seq_no", "biz_seq_no"),
        Index("ix_recon_src_wedap_bank_biz_seq_no", "bank_biz_seq_no"),
        # ix_recon_result_source_wedap_tenant_id 由 TenantMixin(index=True) 自动生成
    )

    id: Mapped[int] = mapped_column(_BIG_PK, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(_BIG_FK, nullable=False)
    biz_type: Mapped[str | None] = mapped_column(String(24))
    biz_seq_no: Mapped[str] = mapped_column(String(32), nullable=False)
    bank_biz_seq_no: Mapped[str | None] = mapped_column(String(64))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(21, 4))
    currency: Mapped[str | None] = mapped_column(String(3))
    payer_account: Mapped[str | None] = mapped_column(String(64))
    payee_account: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str | None] = mapped_column(String(16))
    error_msg: Mapped[str | None] = mapped_column(String(256))


class ReconResultSourceBank(Base, TenantMixin, TimestampMixin):
    """银行侧对账源数据表。"""

    __tablename__ = "recon_result_source_bank"
    __table_args__ = (
        Index("ix_recon_src_bank_task_id", "task_id"),
        Index("ix_recon_src_bank_bank_seq_no", "bank_seq_no"),
        # ix_recon_result_source_bank_tenant_id 由 TenantMixin(index=True) 自动生成
    )

    id: Mapped[int] = mapped_column(_BIG_PK, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(_BIG_FK, nullable=False)
    bank_seq_no: Mapped[str] = mapped_column(String(64), nullable=False)
    txn_date: Mapped[str | None] = mapped_column(String(8))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(21, 4))
    currency: Mapped[str | None] = mapped_column(String(3))
    payer_account: Mapped[str | None] = mapped_column(String(64))
    payee_account: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str | None] = mapped_column(String(16))
    file_name: Mapped[str | None] = mapped_column(String(128))
    line_no: Mapped[int | None] = mapped_column(Integer)
