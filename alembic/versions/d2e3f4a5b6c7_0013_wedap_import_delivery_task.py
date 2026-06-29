"""0013_wedap_import_delivery_task

Revision ID: d2e3f4a5b6c7
Revises: c2d3e4f5a6b7
Create Date: 2026-06-26 12:00:00.000000

wedap flow-import 投递执行账本（§4.3「A+异步回执」· gateway 侧执行账本，非业务权威态）。
新增 wedap_import_delivery_task：recon enqueue 交单后 gateway 记任务，dispatcher 扫
PENDING → 取 staging 字节 → upload+notify → 终态回执 recon。

幂等：(tenant_id, import_batch_no) + (tenant_id, request_id) 双唯一约束。
Forward/backward 都做。
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "d2e3f4a5b6c7"
down_revision: Union[str, Sequence[str], None] = "c2d3e4f5a6b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "wedap_import_delivery_task",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("request_id", sa.String(96), nullable=False),
        sa.Column("import_batch_no", sa.String(64), nullable=False),
        sa.Column("data_type", sa.String(32), nullable=False),
        sa.Column("import_date", sa.String(8), nullable=False),
        sa.Column("staging_key", sa.String(512), nullable=False),
        sa.Column("file_checksum", sa.String(64), nullable=False),
        sa.Column("file_size", sa.BigInteger, nullable=False),
        sa.Column("total_count", sa.Integer, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint("tenant_id", "import_batch_no", name="uq_wedap_delivery_batch"),
        sa.UniqueConstraint("tenant_id", "request_id", name="uq_wedap_delivery_request"),
    )
    op.create_index("ix_wedap_delivery_tenant_id", "wedap_import_delivery_task", ["tenant_id"])
    op.create_index(
        "ix_wedap_delivery_status_retry",
        "wedap_import_delivery_task",
        ["status", "next_retry_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_wedap_delivery_status_retry", table_name="wedap_import_delivery_task")
    op.drop_index("ix_wedap_delivery_tenant_id", table_name="wedap_import_delivery_task")
    op.drop_table("wedap_import_delivery_task")
