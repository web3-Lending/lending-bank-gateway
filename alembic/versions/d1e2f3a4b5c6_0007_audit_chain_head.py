"""0007_audit_chain_head

Revision ID: d1e2f3a4b5c6
Revises: c7d8e9f0a1b2
Create Date: 2026-06-16 09:00:00.000000

per-tenant 审计链尾锚点表（A-m-003）：
  audit_chain_head(tenant_id PK, last_row_hash) —— write_audit 按主键 FOR UPDATE 锁定该行
  串行化同 tenant 审计追加，杜绝并发链分叉，且主键精确锁不产生间隙锁（不触发 1213 死锁）。
  本表会被 UPDATE，故不挂 audit_log 的 append-only 触发器。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, Sequence[str], None] = "c7d8e9f0a1b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "audit_chain_head",
        sa.Column("tenant_id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("last_row_hash", sa.String(64), nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("audit_chain_head")
