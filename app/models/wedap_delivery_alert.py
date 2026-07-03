"""§6.1 护栏③④：wedap 投递 silent-failure 去重告警标记表。

复刻 OrderStuckAlert（G6）模式：UNIQUE 去重 + 限频 ERROR + admin 查询。两类告警：
  - PENDING_STUCK（护栏③）：任务停留 PENDING/SENDING 超过 pending_max_age 仍未投出。
  - RESULT_OVERDUE（护栏④）：已受理（DELIVERED）但超过 result_deadline_at 仍未回收 _result.json，
    切流负责人须按 §6.1 评估回滚（关 GW_WEDAP_DELIVERY_ENABLED / 摘 secret）。
"""

import datetime as dt

from sqlalchemy import BigInteger, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin

_BIG_PK = BigInteger().with_variant(Integer, "sqlite")

WEDAP_DELIVERY_ALERT_KINDS = ("PENDING_STUCK", "RESULT_OVERDUE")


class WedapDeliveryAlert(Base, TenantMixin, TimestampMixin):
    """wedap 投递告警标记（同批同类只告警一次）。

    UNIQUE(tenant_id, import_batch_no, kind) 去重——dispatcher 每轮重复检测同一批次
    只告警一次（插入命中唯一约束即静默跳过），避免 ERROR 每轮刷屏；admin 可查待人工处置。
    """

    __tablename__ = "wedap_delivery_alert"
    __table_args__ = (
        UniqueConstraint("tenant_id", "import_batch_no", "kind", name="uq_wedap_delivery_alert"),
    )

    id: Mapped[int] = mapped_column(_BIG_PK, primary_key=True, autoincrement=True)
    import_batch_no: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # 告警时刻的任务快照摘要（status/attempts/deadline 等），纯诊断用途。
    detail: Mapped[str | None] = mapped_column(String(255))
    first_alerted_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
