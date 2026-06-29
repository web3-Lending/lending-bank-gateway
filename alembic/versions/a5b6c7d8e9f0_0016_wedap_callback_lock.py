"""0016_wedap_callback_lock

Revision ID: a5b6c7d8e9f0
Revises: f4a5b6c7d8e9
Create Date: 2026-06-29 12:00:00.000000

wedap_import_delivery_task 加 callback_locked_at（回执重发原子 claim，codex 复评 P1）：
resend 锁定未送达回执后才重发，多实例只一个抢到；残留锁超时回收。

Forward/backward 都做。
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "a5b6c7d8e9f0"
down_revision: Union[str, Sequence[str], None] = "f4a5b6c7d8e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "wedap_import_delivery_task",
        sa.Column("callback_locked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("wedap_import_delivery_task", "callback_locked_at")
