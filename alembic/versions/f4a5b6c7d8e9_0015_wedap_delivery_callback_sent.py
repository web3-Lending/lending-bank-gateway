"""0015_wedap_delivery_callback_sent

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
Create Date: 2026-06-29 12:00:00.000000

wedap_import_delivery_task 加 callback_sent_at（gateway→recon 回执送达时刻）——终态 + 此列空
= 回执未送达，resend_pending_callbacks_once 重发兜底（FU-WEDAP-CALLBACK-DURABLE）。

Forward/backward 都做。
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "f4a5b6c7d8e9"
down_revision: Union[str, Sequence[str], None] = "e3f4a5b6c7d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "wedap_import_delivery_task",
        sa.Column("callback_sent_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("wedap_import_delivery_task", "callback_sent_at")
