"""0019_drop_bank_txn_leg

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-07-14 15:00:00.000000

按 C5 口径（wedap 组合交易 leg/step 对 recon/lending 透明，对账只在 order 业务级，
绝不下钻 leg）拆除 leg 下钻：删除 bank_txn_leg 表。父单终态收口改走
同步响应 txnStatus + 回调 body txnStatus + status-query（order 级），不再聚合 leg。
跨库消费方（recon GatewayOrderCollector）只读 bank_txn_order / order_stuck_alert，
无 leg 依赖（2026-07-14 全仓核实）。

Forward/backward 都做（downgrade 重建空表，历史 leg 数据不可恢复——dev 环境该表本就零落盘）。
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "b8c9d0e1f2a3"
down_revision: Union[str, Sequence[str], None] = "a7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index(op.f("ix_bank_txn_leg_tenant_id"), table_name="bank_txn_leg")
    op.drop_table("bank_txn_leg")


def downgrade() -> None:
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
