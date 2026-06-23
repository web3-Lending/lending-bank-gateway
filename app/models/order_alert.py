"""G6：超期未收敛父单告警标记表。"""

import datetime as dt

from sqlalchemy import BigInteger, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin

_BIG_PK = BigInteger().with_variant(Integer, "sqlite")


class OrderStuckAlert(Base, TenantMixin, TimestampMixin):
    """超 max_age 仍未收敛终态的父单告警标记。

    UNIQUE(tenant_id, biz_seq_no) 去重——order_reconcile worker 每轮重复检测同一 stuck 单
    只告警一次（插入命中唯一约束即静默跳过），避免 ERROR 日志每轮刷屏；admin 可查待人工处置。
    """

    __tablename__ = "order_stuck_alert"
    __table_args__ = (UniqueConstraint("tenant_id", "biz_seq_no", name="uq_stuck_tenant_biz"),)

    id: Mapped[int] = mapped_column(_BIG_PK, primary_key=True, autoincrement=True)
    biz_seq_no: Mapped[str] = mapped_column(String(32), nullable=False)
    biz_type: Mapped[str | None] = mapped_column(String(8))
    first_alerted_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
