from typing import Any

from sqlalchemy import JSON, BigInteger, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin

# 与 txn.py 保持一致：SQLite 降级为 Integer，MySQL 生产用 BigInteger
_BIG_PK = BigInteger().with_variant(Integer, "sqlite")


class IdempotencyRecord(Base, TenantMixin, TimestampMixin):
    __tablename__ = "idempotency_record"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "business_scope",
            "idempotency_key",
            name="uq_idem_triple",
        ),
    )

    id: Mapped[int] = mapped_column(_BIG_PK, primary_key=True, autoincrement=True)
    business_scope: Mapped[str] = mapped_column(String(48), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    path: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    first_response: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    final_effect_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
