"""wedap flow-import 投递执行账本（§4.3「A+异步回执」· gateway 侧）。

recon 经 enqueue API 交单后，gateway 在本表记一条投递任务（执行日志 + 幂等表，
**非业务权威状态**——权威态在 recon 的 wedap_export_batch）。dispatcher 扫 PENDING
→ 按 staging_key 取字节 → 校 checksum → upload wedap bucket + notify → 终态回执 recon。

幂等（§4.4 + 服务间）：
- (tenant_id, import_batch_no) 唯一——gateway 重试复用同号同 checksum，不生成新号。
- (tenant_id, request_id) 唯一——recon→gateway enqueue 的 request_id 幂等。
"""

import datetime as dt

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin

_BIG_PK = BigInteger().with_variant(Integer, "sqlite")

# 执行状态机：接单 → 投递中 → 完成 / 失败（南向投递终态）。
WEDAP_DELIVERY_STATUS = ("PENDING", "SENDING", "DELIVERED", "FAILED")


class WedapImportDeliveryTask(Base, TenantMixin, TimestampMixin):
    """gateway 侧 wedap 投递执行任务（执行账本 + 幂等，非业务权威态）。"""

    __tablename__ = "wedap_import_delivery_task"
    __table_args__ = (
        UniqueConstraint("tenant_id", "import_batch_no", name="uq_wedap_delivery_batch"),
        UniqueConstraint("tenant_id", "request_id", name="uq_wedap_delivery_request"),
        # dispatcher 按 (status, next_retry_at) 扫待投递任务。
        Index("ix_wedap_delivery_status_retry", "status", "next_retry_at"),
    )

    id: Mapped[int] = mapped_column(_BIG_PK, primary_key=True, autoincrement=True)
    # recon→gateway enqueue 幂等键（wedap-import-{importBatchNo}）。
    request_id: Mapped[str] = mapped_column(String(96), nullable=False)
    import_batch_no: Mapped[str] = mapped_column(String(64), nullable=False)
    data_type: Mapped[str] = mapped_column(String(32), nullable=False)
    import_date: Mapped[str] = mapped_column(String(8), nullable=False)  # YYYYMMDD
    staging_key: Mapped[str] = mapped_column(String(512), nullable=False)  # staging 字节落点
    file_checksum: Mapped[str] = mapped_column(String(64), nullable=False)  # SHA-256（含 header）
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    # 原子 claim 时刻：dispatch 把 PENDING→SENDING 时置；多副本并发只一个抢到，
    # 崩溃残留的 SENDING 由 reclaim（locked_at 超时）回 PENDING 重试。
    locked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    # wedap notify 成功时刻（南向投递完成），非 gateway→recon 回执时刻（回执由 on_terminal 另发）。
    notified_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    # gateway→recon 回执送达时刻（FU-WEDAP-CALLBACK-DURABLE）：终态 + 此列为空 = 回执未送达，
    # resend_pending_callbacks_once 重发兜底，消除 recon 永久卡 ENQUEUED。
    callback_sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    # 回执重发原子 claim 时刻：resend 锁定未送达回执后才重发，多实例只一个抢到；
    # 残留锁超时由 claim WHERE(锁空或超时)回收。codex 复评 P1。
    callback_locked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    # wedap 写回 _result.json 已回收并转投 recon line-results 的时刻（result 回收 Phase1）：
    # status=DELIVERED + 此列为空 = 结果未回收，collect_results_once 轮询拉取兜底。
    result_collected_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    # 结果回收原子 claim 时刻：多实例并发只一个抢到拉取+转投；result 未就绪(404)立即释放锁，
    # 残留锁超时由 claim WHERE(锁空或超时)回收。
    result_locked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
