from enum import StrEnum

from sqlalchemy import CheckConstraint, ForeignKey, Index, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    Base,
    created_at,
    required_text,
    status_in,
    timestamptz,
    uuid_fk,
    uuid_pk,
)


class ReservationStatus(StrEnum):
    HELD = "held"
    PAYING = "paying"
    PAID = "paid"
    EXPIRED = "expired"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Статусы, при которых бронь занимает единицу товара. Именно по ним работает
# ограничение «одна единица на покупателя» (ADR-008).
ACTIVE_RESERVATION_STATUSES = (
    ReservationStatus.HELD,
    ReservationStatus.PAYING,
    ReservationStatus.PAID,
)

_ACTIVE_STATUSES_SQL = ", ".join(f"'{status.value}'" for status in ACTIVE_RESERVATION_STATUSES)


class Reservation(Base):
    __tablename__ = "reservations"
    __table_args__ = (
        CheckConstraint(status_in("status", ReservationStatus), name="status_known"),
        # Частичный уникальный индекс: лимит «одна активная бронь на покупателя
        # в распродаже». Завершённые брони (expired/failed/cancelled) не мешают
        # купить снова.
        Index(
            "uq_reservations_sale_id_user_id_active",
            "sale_id",
            "user_id",
            unique=True,
            postgresql_where=text(f"status IN ({_ACTIVE_STATUSES_SQL})"),
        ),
        # Для свипера: ищет held с истёкшим expires_at.
        Index("ix_reservations_status_expires_at", "status", "expires_at"),
    )

    id: Mapped[uuid_pk]
    sale_id: Mapped[uuid_fk] = mapped_column(ForeignKey("sales.id"))
    user_id: Mapped[uuid_fk] = mapped_column(ForeignKey("users.id"))
    status: Mapped[required_text]
    # created_at + TTL брони. Срок задаёт сервис, а не схема: инвариант 3 —
    # бизнес-правило, менять его миграцией было бы неудобно.
    # Для статуса paying игнорируется (инвариант 4).
    expires_at: Mapped[timestamptz]
    created_at: Mapped[created_at]
    updated_at: Mapped[created_at] = mapped_column(onupdate=text("now()"))
