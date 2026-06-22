"""0010_order_finalized_via

Revision ID: a3b4c5d6e7f8
Revises: a4b5c6d7e8f9
Create Date: 2026-06-17 10:00:00.000000

同步优先终态收口（V2）：bank_txn_order 新增
  - finalized_via VARCHAR(12) NULL —— 终态来源留痕 SYNC/CALLBACK/RECONCILE，
    给 recon 对账「同步 vs 回调谁收口」+ 排查重复 webhook 用。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3b4c5d6e7f8"
down_revision: Union[str, Sequence[str], None] = "a4b5c6d7e8f9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "bank_txn_order",
        sa.Column("finalized_via", sa.String(12), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("bank_txn_order", "finalized_via")
