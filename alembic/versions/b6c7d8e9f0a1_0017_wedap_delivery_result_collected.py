"""0017_wedap_delivery_result_collected

Revision ID: b6c7d8e9f0a1
Revises: a5b6c7d8e9f0
Create Date: 2026-06-30 16:00:00.000000

wedap_import_delivery_task 加 result_collected_at + result_locked_at（result 回收 Phase1）——
status=DELIVERED + result_collected_at 空 = wedap _result.json 未回收，collect_results_once
轮询拉取 → parse_result → 转投 recon line-results；result_locked_at 多实例并发 claim。

Forward/backward 都做。
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "b6c7d8e9f0a1"
down_revision: Union[str, Sequence[str], None] = "a5b6c7d8e9f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "wedap_import_delivery_task",
        sa.Column("result_collected_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "wedap_import_delivery_task",
        sa.Column("result_locked_at", sa.DateTime(timezone=True), nullable=True),
    )
    # ownership token（UUID hex）：mark/release 按 token 匹配，避 MySQL DATETIME 截微秒时间戳
    # 等值永不匹配（codex P1 二轮 HIGH-1）
    op.add_column(
        "wedap_import_delivery_task",
        sa.Column("result_lock_token", sa.String(length=32), nullable=True),
    )
    # collect_results_once 扫描路径 (status=DELIVERED, result_collected_at IS NULL)（codex P1 MEDIUM-2）
    op.create_index(
        "ix_wedap_delivery_result_scan",
        "wedap_import_delivery_task",
        ["status", "result_collected_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_wedap_delivery_result_scan", table_name="wedap_import_delivery_task")
    op.drop_column("wedap_import_delivery_task", "result_lock_token")
    op.drop_column("wedap_import_delivery_task", "result_locked_at")
    op.drop_column("wedap_import_delivery_task", "result_collected_at")
