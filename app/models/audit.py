"""app/models/audit.py

审计日志：
- AuditLog — append-only 操作日志（application 只 INSERT；
  MySQL 层 permission/trigger 禁止 UPDATE/DELETE，部署时实施）
"""

from typing import Any

from sqlalchemy import JSON, BigInteger, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin

# SQLite 的 autoincrement 只支持 INTEGER 类型；MySQL 生产环境需要 BIGINT。
# with_variant 在 SQLite 测试中降级为 Integer，在 MySQL 中保持 BigInteger。
_BIG_PK = BigInteger().with_variant(Integer, "sqlite")


class AuditLog(Base, TenantMixin, TimestampMixin):
    """审计日志表（append-only：application 只 INSERT；
    MySQL BEFORE UPDATE/DELETE 触发器拒绝修改，migration 0005 创建）。

    uq_audit_tenant_rowhash：(tenant_id, row_hash) 唯一——防止同一租户重复写入相同哈希行。
    """

    __tablename__ = "audit_log"
    __table_args__ = (UniqueConstraint("tenant_id", "row_hash", name="uq_audit_tenant_rowhash"),)

    id: Mapped[int] = mapped_column(_BIG_PK, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(48), nullable=False)
    entity: Mapped[str] = mapped_column(String(64), nullable=False)
    # 只写不改：直接修改 dict 不触发 dirty tracking
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    row_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class AuditChainHead(Base):
    """per-tenant 审计链尾锚点（A-m-003 并发串行化）。

    write_audit 先幂等确保该 tenant 锚点行存在，再按主键 `FOR UPDATE` 精确锁定该行
    （主键精确锁不产生间隙锁 → 无 1213 死锁），读 last_row_hash 作为 prev，写 audit_log 后
    回写 last_row_hash。锁随调用方事务提交释放，per-tenant 串行化，杜绝链分叉。

    注意：本表会被 UPDATE（与 audit_log append-only 不同），故不挂 append-only 触发器。
    """

    __tablename__ = "audit_chain_head"

    tenant_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    last_row_hash: Mapped[str] = mapped_column(String(64), nullable=False)
