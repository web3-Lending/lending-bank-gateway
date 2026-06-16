"""0008_outbox_claim_trace

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-06-16 10:00:00.000000

outbox 原子 claim + trace 透传（A-M-001 / A-m-001）：
  callback_outbox 新增
    - locked_at  DATETIME(tz) NULL —— 多副本原子 claim 置 SENDING + locked_at，
      超时未完成由 dispatcher reclaim 回 FAILED 重试
    - trace_id   VARCHAR(64) NULL —— 触发该转发的原始 trace_id，dispatcher 透传给下游
  status 枚举新增 SENDING 中间态（无需 DDL，字符串列）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2f3a4b5c6d7"
down_revision: Union[str, Sequence[str], None] = "d1e2f3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "callback_outbox",
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "callback_outbox",
        sa.Column("trace_id", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("callback_outbox", "trace_id")
    op.drop_column("callback_outbox", "locked_at")
