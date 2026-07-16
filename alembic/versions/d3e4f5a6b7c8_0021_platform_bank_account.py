"""0021_platform_bank_account

Revision ID: d3e4f5a6b7c8
Revises: c9d0e1f2a3b4
Create Date: 2026-07-16 17:30:00.000000

平台账户白名单表（account guard 一期，FU-GW-ACCOUNT-GUARD-20260616-001）：
collect/distribute 的平台账户 enforcement 依据——非账户主数据。
初始 seed 由资金运营提供清单后走独立 data migration / admin 端点，本迁移只建结构。

Forward/backward 都做。
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "d3e4f5a6b7c8"
down_revision: Union[str, Sequence[str], None] = "c9d0e1f2a3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "platform_bank_account",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("account_no", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(24), nullable=False),
        sa.Column("allowed_scopes", sa.String(255), nullable=False),
        sa.Column("currency", sa.String(3), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("note", sa.String(255), nullable=True),
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
        sa.UniqueConstraint("tenant_id", "account_no", name="uq_platform_account_tenant_acct"),
    )
    op.create_index("ix_platform_bank_account_tenant_id", "platform_bank_account", ["tenant_id"])
    # MySQL 专属（对齐 0002 惯例）：updated_at 补 ON UPDATE CURRENT_TIMESTAMP，
    # 使 data migration / raw SQL 改行也刷新时间戳，与 ORM onupdate 语义一致
    if op.get_bind().dialect.name == "mysql":  # pragma: no cover
        op.execute(
            "ALTER TABLE platform_bank_account MODIFY updated_at DATETIME NOT NULL"
            " DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"
        )


def downgrade() -> None:
    op.drop_index("ix_platform_bank_account_tenant_id", table_name="platform_bank_account")
    op.drop_table("platform_bank_account")
