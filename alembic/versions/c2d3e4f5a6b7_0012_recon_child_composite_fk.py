"""0012_recon_child_composite_fk

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-06-23 12:30:00.000000

G4：recon 三张子表（diff / source_wedap / source_bank）补复合 FK (task_id, tenant_id) →
recon_result_task(id, tenant_id)，把跨租户/孤儿引用从应用层兜底升级为 DB 层物理拦截。

顺序（codex 指定）：① 脏数据硬门禁（孤儿/跨租户）→ ② 父表复合唯一 → ③ 子表复合索引 → ④ FK。
单列 task_id 索引被复合索引（task_id 最左前缀）替代后删除。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c2d3e4f5a6b7"
down_revision: Union[str, Sequence[str], None] = "b1c2d3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (child_table, fk_name, old_single_idx, new_composite_idx)
_CHILDREN = (
    (
        "recon_result_diff",
        "fk_recon_diff_task",
        "ix_recon_diff_task_id",
        "ix_recon_diff_task_tenant",
    ),
    (
        "recon_result_source_wedap",
        "fk_recon_src_wedap_task",
        "ix_recon_src_wedap_task_id",
        "ix_recon_src_wedap_task_tenant",
    ),
    (
        "recon_result_source_bank",
        "fk_recon_src_bank_task",
        "ix_recon_src_bank_task_id",
        "ix_recon_src_bank_task_tenant",
    ),
)


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()
    # ① 脏数据硬门禁：孤儿 task_id 或跨租户引用（c.tenant_id 与父 task.tenant_id 不配对）→ 中止迁移
    for child, _fk, _old, _new in _CHILDREN:
        orphan = conn.execute(
            sa.text(
                f"SELECT COUNT(*) FROM {child} c "  # noqa: S608 - 表名来自内部常量白名单
                "LEFT JOIN recon_result_task t "
                "ON c.task_id = t.id AND c.tenant_id = t.tenant_id "
                "WHERE t.id IS NULL"
            )
        ).scalar()
        if orphan:
            raise RuntimeError(
                f"0012 迁移中止：{child} 有 {orphan} 行孤儿/跨租户 task_id 引用，"
                "请先清理（LEFT JOIN recon_result_task ON id+tenant_id 找 NULL 行）后重跑。"
            )
    # ② 父表复合唯一 (id, tenant_id)：作为子表复合 FK 的引用目标
    op.create_unique_constraint("uq_recon_task_id_tenant", "recon_result_task", ["id", "tenant_id"])
    # ③ 子表：复合索引（task_id 最左前缀）→ ④ FK → 删旧单列索引
    for child, fk_name, old_idx, new_idx in _CHILDREN:
        op.create_index(new_idx, child, ["task_id", "tenant_id"], unique=False)
        op.create_foreign_key(
            fk_name,
            child,
            "recon_result_task",
            ["task_id", "tenant_id"],
            ["id", "tenant_id"],
            ondelete="RESTRICT",
        )
        op.drop_index(old_idx, table_name=child)


def downgrade() -> None:
    """Downgrade schema."""
    for child, fk_name, old_idx, new_idx in _CHILDREN:
        op.create_index(old_idx, child, ["task_id"], unique=False)
        op.drop_constraint(fk_name, child, type_="foreignkey")
        op.drop_index(new_idx, table_name=child)
    op.drop_constraint("uq_recon_task_id_tenant", "recon_result_task", type_="unique")
