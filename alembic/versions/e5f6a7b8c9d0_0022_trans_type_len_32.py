"""0022_trans_type_len_32

Revision ID: e5f6a7b8c9d0
Revises: d3e4f5a6b7c8
Create Date: 2026-07-24 16:00:00.000000

bank_txn_order.trans_type 20→32：wedap 2026-07-24 定稿 transType 权威字典并把字段上限
修订为 32（BANK_FUND_COLLECT_CLEARING=26 超旧限 20）。入口 schema 同步 20→32。

batch_alter_table 兼容 sqlite（env.py 显式支持 aiosqlite 路径；MySQL 下退化为普通
ALTER，VARCHAR 同字节类内加宽 INPLACE 安全）。downgrade 收窄回 20 前置校验无超长
存量值——有则 fail-fast 拒绝降级，防 MySQL 静默截断造成「回查值 != 提交值」永久查不到；
长度函数按方言取 CHAR_LENGTH（MySQL）/ LENGTH（sqlite，TEXT 计字符）。
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, Sequence[str], None] = "d3e4f5a6b7c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("bank_txn_order") as batch_op:
        batch_op.alter_column(
            "trans_type",
            existing_type=sa.String(length=20),
            type_=sa.String(length=32),
            existing_nullable=True,
        )


def downgrade() -> None:
    conn = op.get_bind()
    length_fn = sa.func.char_length if conn.dialect.name == "mysql" else sa.func.length
    trans_type = sa.column("trans_type", sa.String())
    overlong = conn.execute(
        sa.select(sa.func.count())
        .select_from(sa.table("bank_txn_order", trans_type))
        .where(length_fn(trans_type) > 20)
    ).scalar()
    if overlong:
        raise RuntimeError(
            f"cannot downgrade trans_type to String(20): {overlong} rows exceed 20 chars"
        )
    with op.batch_alter_table("bank_txn_order") as batch_op:
        batch_op.alter_column(
            "trans_type",
            existing_type=sa.String(length=32),
            type_=sa.String(length=20),
            existing_nullable=True,
        )
