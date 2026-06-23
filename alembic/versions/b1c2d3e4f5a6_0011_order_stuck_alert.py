"""0011_order_stuck_alert

Revision ID: b1c2d3e4f5a6
Revises: a3b4c5d6e7f8
Create Date: 2026-06-23 12:00:00.000000

G6：超期未收敛父单告警标记表 order_stuck_alert。
UNIQUE(tenant_id, biz_seq_no) 去重——order_reconcile worker 每轮重复检测同一 stuck 单
只告警一次（插入命中唯一约束即静默跳过），避免 ERROR 日志每轮刷屏；admin 可查待人工处置。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, Sequence[str], None] = "a3b4c5d6e7f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "order_stuck_alert",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("biz_seq_no", sa.String(length=32), nullable=False),
        sa.Column("biz_type", sa.String(length=8), nullable=True),
        sa.Column("first_alerted_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.UniqueConstraint("tenant_id", "biz_seq_no", name="uq_stuck_tenant_biz"),
    )
    op.create_index(
        "ix_order_stuck_alert_tenant_id", "order_stuck_alert", ["tenant_id"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_order_stuck_alert_tenant_id", table_name="order_stuck_alert")
    op.drop_table("order_stuck_alert")
