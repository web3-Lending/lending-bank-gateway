"""0004_recon_audit_tables

Revision ID: a1b2c3d4e5f6
Revises: 0df9f359346b
Create Date: 2026-06-11 20:00:00.000000

新增 7 张表：
  recon_result_task, recon_result_diff, recon_result_source_wedap,
  recon_result_source_bank, query_audit, balance_snapshot, audit_log

callback_outbox 索引升级（T7 residual）：
  - 删除旧单列 ix_callback_outbox_status
  - 创建复合索引 ix_callback_outbox_status_retry (status, next_retry_at)

callback_inbox 补默认值（T7 residual）：
  - status 列补 server_default='RECEIVED'

MySQL 专属：新表 updated_at 补 ON UPDATE CURRENT_TIMESTAMP（对称 downgrade）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "0df9f359346b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 新表中包含 TimestampMixin（updated_at）的表名列表
_ON_UPDATE_TABLES = (
    "recon_result_task",
    "recon_result_diff",
    "recon_result_source_wedap",
    "recon_result_source_bank",
    "query_audit",
    "balance_snapshot",
    "audit_log",
)

_MYSQL_UPGRADE_TMPL = (
    "ALTER TABLE {table} MODIFY updated_at DATETIME NOT NULL"
    " DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"
)
_MYSQL_DOWNGRADE_TMPL = (
    "ALTER TABLE {table} MODIFY updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP"
)


def upgrade() -> None:
    """Upgrade schema."""
    # ── recon_result_task ────────────────────────────────────────────────────
    op.create_table(
        "recon_result_task",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("task_no", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("recon_date", sa.String(length=8), nullable=False),
        sa.Column("s3_bucket", sa.String(length=128), nullable=False),
        sa.Column("s3_key", sa.String(length=256), nullable=False),
        sa.Column("file_md5", sa.String(length=32), nullable=False),
        sa.Column("diff_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("parser_version", sa.String(length=16), nullable=True),
        sa.Column("schema_version", sa.String(length=16), nullable=True),
        sa.Column("column_check", sa.JSON(), nullable=True),
        sa.Column("archive_path", sa.String(length=256), nullable=True),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "request_id", name="uq_recon_task_req"),
        sa.UniqueConstraint("tenant_id", "task_no", "version", name="uq_recon_task_ver"),
    )
    op.create_index(
        "ix_recon_result_task_tenant_id", "recon_result_task", ["tenant_id"], unique=False
    )

    # ── recon_result_diff ────────────────────────────────────────────────────
    op.create_table(
        "recon_result_diff",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("task_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("diff_type", sa.String(length=8), nullable=False),
        sa.Column("wedap_biz_seq_no", sa.String(length=32), nullable=True),
        sa.Column("bank_seq_no", sa.String(length=64), nullable=True),
        sa.Column("wedap_amount", sa.Numeric(precision=21, scale=4), nullable=True),
        sa.Column("bank_amount", sa.Numeric(precision=21, scale=4), nullable=True),
        sa.Column("diff_amount", sa.Numeric(precision=21, scale=4), nullable=True),
        sa.Column("wedap_status", sa.String(length=16), nullable=True),
        sa.Column("bank_status", sa.String(length=16), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_recon_diff_task_id", "recon_result_diff", ["task_id"], unique=False)
    op.create_index(
        "ix_recon_diff_wedap_biz_seq_no",
        "recon_result_diff",
        ["wedap_biz_seq_no"],
        unique=False,
    )
    op.create_index("ix_recon_diff_bank_seq_no", "recon_result_diff", ["bank_seq_no"], unique=False)

    # ── recon_result_source_wedap ────────────────────────────────────────────
    op.create_table(
        "recon_result_source_wedap",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("task_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("biz_type", sa.String(length=24), nullable=True),
        sa.Column("biz_seq_no", sa.String(length=32), nullable=False),
        sa.Column("bank_biz_seq_no", sa.String(length=64), nullable=True),
        sa.Column("amount", sa.Numeric(precision=21, scale=4), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("payer_account", sa.String(length=64), nullable=True),
        sa.Column("payee_account", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=True),
        sa.Column("error_msg", sa.String(length=256), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_recon_src_wedap_task_id", "recon_result_source_wedap", ["task_id"], unique=False
    )
    op.create_index(
        "ix_recon_src_wedap_biz_seq_no",
        "recon_result_source_wedap",
        ["biz_seq_no"],
        unique=False,
    )
    op.create_index(
        "ix_recon_src_wedap_bank_biz_seq_no",
        "recon_result_source_wedap",
        ["bank_biz_seq_no"],
        unique=False,
    )

    # ── recon_result_source_bank ─────────────────────────────────────────────
    op.create_table(
        "recon_result_source_bank",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("task_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("bank_seq_no", sa.String(length=64), nullable=False),
        sa.Column("txn_date", sa.String(length=8), nullable=True),
        sa.Column("amount", sa.Numeric(precision=21, scale=4), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("payer_account", sa.String(length=64), nullable=True),
        sa.Column("payee_account", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=True),
        sa.Column("file_name", sa.String(length=128), nullable=True),
        sa.Column("line_no", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_recon_src_bank_task_id", "recon_result_source_bank", ["task_id"], unique=False
    )
    op.create_index(
        "ix_recon_src_bank_bank_seq_no",
        "recon_result_source_bank",
        ["bank_seq_no"],
        unique=False,
    )

    # ── query_audit ──────────────────────────────────────────────────────────
    op.create_table(
        "query_audit",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("endpoint", sa.String(length=128), nullable=False),
        sa.Column("params_hash", sa.String(length=64), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("caller_service", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    # ── balance_snapshot ─────────────────────────────────────────────────────
    op.create_table(
        "balance_snapshot",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("account_id", sa.String(length=64), nullable=False),
        sa.Column("balance", sa.Numeric(precision=21, scale=4), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("source_endpoint", sa.String(length=128), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_balance_snapshot_account_id", "balance_snapshot", ["account_id"], unique=False
    )

    # ── audit_log ────────────────────────────────────────────────────────────
    op.create_table(
        "audit_log",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=48), nullable=False),
        sa.Column("entity", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("prev_hash", sa.String(length=64), nullable=False),
        sa.Column("row_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    # ── callback_outbox 索引升级（T7 residual）───────────────────────────────
    # 删除旧单列索引，创建复合索引 (status, next_retry_at)
    op.drop_index("ix_callback_outbox_status", table_name="callback_outbox")
    op.create_index(
        "ix_callback_outbox_status_retry",
        "callback_outbox",
        ["status", "next_retry_at"],
        unique=False,
    )

    # ── callback_inbox status 补 server_default（T7 residual）────────────────
    if op.get_bind().dialect.name == "mysql":  # pragma: no cover
        op.execute(
            "ALTER TABLE callback_inbox MODIFY status VARCHAR(16) NOT NULL DEFAULT 'RECEIVED'"
        )

    # ── MySQL 专属：新表 updated_at 补 ON UPDATE CURRENT_TIMESTAMP ───────────
    if op.get_bind().dialect.name == "mysql":  # pragma: no cover
        for table in _ON_UPDATE_TABLES:
            op.execute(_MYSQL_UPGRADE_TMPL.format(table=table))


def downgrade() -> None:
    """Downgrade schema."""
    # ── MySQL 专属：还原 updated_at（去掉 ON UPDATE）────────────────────────
    if op.get_bind().dialect.name == "mysql":  # pragma: no cover
        for table in reversed(_ON_UPDATE_TABLES):
            op.execute(_MYSQL_DOWNGRADE_TMPL.format(table=table))

    # ── callback_inbox status 还原（去掉 DEFAULT）────────────────────────────
    if op.get_bind().dialect.name == "mysql":  # pragma: no cover
        op.execute("ALTER TABLE callback_inbox MODIFY status VARCHAR(16) NOT NULL")

    # ── callback_outbox 索引回滚 ─────────────────────────────────────────────
    op.drop_index("ix_callback_outbox_status_retry", table_name="callback_outbox")
    op.create_index("ix_callback_outbox_status", "callback_outbox", ["status"], unique=False)

    # ── 删除新增 7 张表（逆序）──────────────────────────────────────────────
    op.drop_table("audit_log")
    op.drop_index("ix_balance_snapshot_account_id", table_name="balance_snapshot")
    op.drop_table("balance_snapshot")
    op.drop_table("query_audit")
    op.drop_index("ix_recon_src_bank_bank_seq_no", table_name="recon_result_source_bank")
    op.drop_index("ix_recon_src_bank_task_id", table_name="recon_result_source_bank")
    op.drop_table("recon_result_source_bank")
    op.drop_index("ix_recon_src_wedap_bank_biz_seq_no", table_name="recon_result_source_wedap")
    op.drop_index("ix_recon_src_wedap_biz_seq_no", table_name="recon_result_source_wedap")
    op.drop_index("ix_recon_src_wedap_task_id", table_name="recon_result_source_wedap")
    op.drop_table("recon_result_source_wedap")
    op.drop_index("ix_recon_diff_bank_seq_no", table_name="recon_result_diff")
    op.drop_index("ix_recon_diff_wedap_biz_seq_no", table_name="recon_result_diff")
    op.drop_index("ix_recon_diff_task_id", table_name="recon_result_diff")
    op.drop_table("recon_result_diff")
    op.drop_index("ix_recon_result_task_tenant_id", table_name="recon_result_task")
    op.drop_table("recon_result_task")
