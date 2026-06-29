"""0014_wedap_delivery_locked_at

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-06-29 10:00:00.000000

wedap_import_delivery_task 加 locked_at（原子 claim 时刻）——dispatch PENDING→SENDING 原子
claim 多副本并发安全 + 崩溃残留 SENDING 由 reclaim 回 PENDING（codex 终审 Major）。

Forward/backward 都做。
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "e3f4a5b6c7d8"
down_revision: Union[str, Sequence[str], None] = "d2e3f4a5b6c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "wedap_import_delivery_task",
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("wedap_import_delivery_task", "locked_at")
