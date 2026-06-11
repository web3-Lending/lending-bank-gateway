"""0001 order leg

Revision ID: f78c3a297e5c
Revises:
Create Date: 2026-06-11 18:11:26.582374

M2 fix: BankTxnOrder 补 uq_order_id_tenant；BankTxnLeg order_id 改为复合 FK（order_id+tenant_id）
        防止 leg 跨租户引用 order。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f78c3a297e5c"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "bank_txn_order",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("biz_seq_no", sa.String(length=32), nullable=False),
        sa.Column("business_action", sa.String(length=32), nullable=False),
        sa.Column("biz_type", sa.String(length=8), nullable=False),
        sa.Column("amount", sa.Numeric(precision=21, scale=4), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("caller_service", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "biz_seq_no", name="uq_order_tenant_biz"),
        # 为 BankTxnLeg 复合 FK 提供父键：(id, tenant_id) 唯一
        sa.UniqueConstraint("id", "tenant_id", name="uq_order_id_tenant"),
    )
    op.create_index(
        op.f("ix_bank_txn_order_tenant_id"), "bank_txn_order", ["tenant_id"], unique=False
    )
    op.create_table(
        "bank_txn_leg",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column(
            "order_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column("biz_seq_no", sa.String(length=32), nullable=False),
        sa.Column("external_system", sa.String(length=16), nullable=False),
        sa.Column("external_ref", sa.String(length=64), nullable=False),
        sa.Column("step_type", sa.String(length=40), nullable=False),
        sa.Column("step_seq", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=21, scale=4), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("payer_account", sa.String(length=64), nullable=True),
        sa.Column("payee_account", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("txn_date", sa.String(length=8), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        # 复合 FK：leg.(order_id, tenant_id) → order.(id, tenant_id)
        # 防跨租户引用：leg 只能指向同一 tenant 的 order
        sa.ForeignKeyConstraint(
            ["order_id", "tenant_id"],
            ["bank_txn_order.id", "bank_txn_order.tenant_id"],
            name="fk_leg_order_tenant",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "biz_seq_no", "step_seq", name="uq_leg_tenant_biz_step"),
        sa.UniqueConstraint(
            "tenant_id", "external_system", "external_ref", name="uq_leg_tenant_ext"
        ),
    )
    op.create_index(op.f("ix_bank_txn_leg_tenant_id"), "bank_txn_leg", ["tenant_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_bank_txn_leg_tenant_id"), table_name="bank_txn_leg")
    op.drop_table("bank_txn_leg")
    op.drop_index(op.f("ix_bank_txn_order_tenant_id"), table_name="bank_txn_order")
    op.drop_table("bank_txn_order")
