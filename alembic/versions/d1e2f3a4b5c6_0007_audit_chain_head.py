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
    # 数据回填（M1）：升级前已有 audit_log 历史的库，用每个 tenant 的链尾 row_hash 初始化锚点，
    # 否则首次 write_audit 会从 GENESIS 重起链、与历史末行断链。
    # 取每 tenant 最大 id 的 row_hash 作链尾。write_audit 另有自愈兜底（锚点缺失时同样从历史链尾 seed）。
    op.execute(
        """
        INSERT INTO audit_chain_head (tenant_id, last_row_hash)
        SELECT a.tenant_id, a.row_hash
        FROM audit_log a
        JOIN (
            SELECT tenant_id, MAX(id) AS max_id
            FROM audit_log
            GROUP BY tenant_id
        ) m ON a.tenant_id = m.tenant_id AND a.id = m.max_id
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("audit_chain_head")
