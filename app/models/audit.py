"""app/models/audit.py

审计日志：
- AuditLog — append-only 操作日志（application 只 INSERT；
  MySQL 层 permission/trigger 禁止 UPDATE/DELETE，部署时实施）
"""

from typing import Any

from sqlalchemy import JSON, BigInteger, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

# SQLite 的 autoincrement 只支持 INTEGER 类型；MySQL 生产环境需要 BIGINT。
# with_variant 在 SQLite 测试中降级为 Integer，在 MySQL 中保持 BigInteger。
_BIG_PK = BigInteger().with_variant(Integer, "sqlite")


class AuditLog(Base, TimestampMixin):
    """审计日志表（append-only：application 只 INSERT；
    MySQL permission/trigger 禁止 UPDATE/DELETE，待部署时实施）。
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(_BIG_PK, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(48), nullable=False)
    entity: Mapped[str] = mapped_column(String(64), nullable=False)
    # 只写不改：直接修改 dict 不触发 dirty tracking
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    row_hash: Mapped[str] = mapped_column(String(64), nullable=False)
