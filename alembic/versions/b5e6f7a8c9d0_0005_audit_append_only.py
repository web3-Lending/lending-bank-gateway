"""0005_audit_append_only

Revision ID: b5e6f7a8c9d0
Revises: a1b2c3d4e5f6
Create Date: 2026-06-11 22:00:00.000000

audit_log append-only 机制：
  1. UniqueConstraint (tenant_id, row_hash) → uq_audit_tenant_rowhash
     防止同一租户重复写入相同哈希行。
  2. MySQL 专属 BEFORE UPDATE / BEFORE DELETE 触发器：
     对任何修改 audit_log 的操作 SIGNAL SQLSTATE '45000'（强制 DB 层 append-only）。
     SQLite 不支持 SIGNAL，测试用 unique 约束验证逻辑层保护。
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b5e6f7a8c9d0"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TRIGGER_BEFORE_UPDATE = """\
CREATE TRIGGER trg_audit_log_no_update
BEFORE UPDATE ON audit_log
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'audit_log is append-only';
END
"""

_TRIGGER_BEFORE_DELETE = """\
CREATE TRIGGER trg_audit_log_no_delete
BEFORE DELETE ON audit_log
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'audit_log is append-only';
END
"""


def upgrade() -> None:
    """Upgrade schema."""
    # 1. unique constraint — 全方言（SQLite + MySQL）
    op.create_unique_constraint("uq_audit_tenant_rowhash", "audit_log", ["tenant_id", "row_hash"])

    # 2. MySQL 专属：BEFORE UPDATE / BEFORE DELETE 触发器
    if op.get_bind().dialect.name == "mysql":  # pragma: no cover
        op.execute(_TRIGGER_BEFORE_UPDATE)
        op.execute(_TRIGGER_BEFORE_DELETE)


def downgrade() -> None:
    """Downgrade schema."""
    # MySQL 专属：先删触发器再删约束
    if op.get_bind().dialect.name == "mysql":  # pragma: no cover
        op.execute("DROP TRIGGER IF EXISTS trg_audit_log_no_update")
        op.execute("DROP TRIGGER IF EXISTS trg_audit_log_no_delete")

    op.drop_constraint("uq_audit_tenant_rowhash", "audit_log", type_="unique")
