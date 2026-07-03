"""0018_wedap_delivery_guardrails

Revision ID: a7b8c9d0e1f2
Revises: b6c7d8e9f0a1
Create Date: 2026-07-03 11:00:00.000000

§6.1 五护栏（切流 silent-failure 防护）：
  - wedap_import_delivery_task 加 accepted_at / result_file_path / result_deadline_at（护栏②：
    batch 受理证据 + result 回收明确截止）。
  - 新表 wedap_delivery_alert（护栏③④：PENDING_STUCK / RESULT_OVERDUE 去重告警标记）。

Forward/backward 都做。
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, Sequence[str], None] = "b6c7d8e9f0a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "wedap_import_delivery_task",
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "wedap_import_delivery_task",
        sa.Column("result_file_path", sa.String(512), nullable=True),
    )
    op.add_column(
        "wedap_import_delivery_task",
        sa.Column("result_deadline_at", sa.DateTime(timezone=True), nullable=True),
    )
    # 护栏③④告警扫描索引（codex MEDIUM）：dispatcher 每轮跑，避免多副本下全表扫。
    op.create_index(
        "ix_wedap_delivery_stuck_scan",
        "wedap_import_delivery_task",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_wedap_delivery_overdue_scan",
        "wedap_import_delivery_task",
        ["status", "result_collected_at", "result_deadline_at"],
    )
    op.create_table(
        "wedap_delivery_alert",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("import_batch_no", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("detail", sa.String(255), nullable=True),
        sa.Column("first_alerted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("tenant_id", "import_batch_no", "kind", name="uq_wedap_delivery_alert"),
    )
    op.create_index("ix_wedap_delivery_alert_tenant_id", "wedap_delivery_alert", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_wedap_delivery_alert_tenant_id", table_name="wedap_delivery_alert")
    op.drop_table("wedap_delivery_alert")
    op.drop_index("ix_wedap_delivery_overdue_scan", table_name="wedap_import_delivery_task")
    op.drop_index("ix_wedap_delivery_stuck_scan", table_name="wedap_import_delivery_task")
    op.drop_column("wedap_import_delivery_task", "result_deadline_at")
    op.drop_column("wedap_import_delivery_task", "result_file_path")
    op.drop_column("wedap_import_delivery_task", "accepted_at")
