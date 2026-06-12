"""0006_outbox_dedup_key

Revision ID: c7d8e9f0a1b2
Revises: b5e6f7a8c9d0
Create Date: 2026-06-11 23:00:00.000000

outbox 幂等键：
  1. callback_outbox 新增 dedup_key VARCHAR(160) NULL 列
  2. UniqueConstraint (tenant_id, target, dedup_key) → uq_outbox_dedup
     NULL 值在 MySQL/SQLite 中不参与唯一约束匹配——可接受，手工 enqueue 无 dedup 需求。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7d8e9f0a1b2"
down_revision: Union[str, Sequence[str], None] = "b5e6f7a8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1. 新增 dedup_key 列（可 NULL）
    op.add_column(
        "callback_outbox",
        sa.Column("dedup_key", sa.String(160), nullable=True),
    )
    # 2. 唯一约束（三元组幂等键）
    op.create_unique_constraint(
        "uq_outbox_dedup",
        "callback_outbox",
        ["tenant_id", "target", "dedup_key"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    # 对称撤销：先删约束，再删列
    op.drop_constraint("uq_outbox_dedup", "callback_outbox", type_="unique")
    op.drop_column("callback_outbox", "dedup_key")
