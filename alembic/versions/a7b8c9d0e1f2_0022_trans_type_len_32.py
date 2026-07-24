"""0022_trans_type_len_32

Revision ID: a7b8c9d0e1f2
Revises: d3e4f5a6b7c8
Create Date: 2026-07-24 16:00:00.000000

bank_txn_order.trans_type 20→32：wedap 2026-07-24 定稿 transType 权威字典并把字段上限
修订为 32（BANK_FUND_COLLECT_CLEARING=26 超旧限 20）。入口 schema 同步 20→32。

downgrade 收窄回 20 前置校验无超长存量值——有则 fail-fast 拒绝降级，
防 MySQL 静默截断造成「回查值 != 提交值」永久查不到。
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, Sequence[str], None] = "d3e4f5a6b7c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "bank_txn_order",
        "trans_type",
        existing_type=sa.String(length=20),
        type_=sa.String(length=32),
        existing_nullable=True,
    )


def downgrade() -> None:
    conn = op.get_bind()
    overlong = conn.execute(
        sa.text("SELECT COUNT(*) FROM bank_txn_order WHERE CHAR_LENGTH(trans_type) > 20")
    ).scalar()
    if overlong:
        raise RuntimeError(
            f"cannot downgrade trans_type to String(20): {overlong} rows exceed 20 chars"
        )
    op.alter_column(
        "bank_txn_order",
        "trans_type",
        existing_type=sa.String(length=32),
        type_=sa.String(length=20),
        existing_nullable=True,
    )
