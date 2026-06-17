"""0009_outbox_claim_token

Revision ID: a4b5c6d7e8f9
Revises: e2f3a4b5c6d7
Create Date: 2026-06-17 20:40:00.000000

outbox 终结状态 CAS 加固（at-least-once 极端边界）：
  callback_outbox 新增
    - claim_token VARCHAR(36) NULL —— 每次原子 claim 生成新 uuid4；终结状态写回用
      CAS 守护（WHERE status=SENDING AND claim_token=本次令牌），防止副本 A claim 后
      stall 超 claim_timeout 被副本 B reclaim 重投，A 迟到成功覆盖 B 终态。
  旧数据行 claim_token=NULL（处于终态/PENDING，无活跃 claim），新 claim 自动生成令牌，
  无需数据回填。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4b5c6d7e8f9"
down_revision: Union[str, Sequence[str], None] = "e2f3a4b5c6d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "callback_outbox",
        sa.Column("claim_token", sa.String(36), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("callback_outbox", "claim_token")
