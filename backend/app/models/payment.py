from enum import StrEnum

from sqlalchemy import CheckConstraint, ForeignKey, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    Base,
    created_at,
    optional_text,
    required_text,
    status_in,
    timestamptz_null,
    uuid_fk,
    uuid_pk,
)


class PaymentStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    DECLINED = "declined"


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (CheckConstraint(status_in("status", PaymentStatus), name="status_known"),)

    id: Mapped[uuid_pk]
    # Инвариант 5: один платёж на заказ.
    order_id: Mapped[uuid_fk] = mapped_column(ForeignKey("orders.id"), unique=True)
    # Передаётся в заглушку: повторный клик не создаёт второй платёж.
    idempotency_key: Mapped[required_text] = mapped_column(unique=True)
    provider_payment_id: Mapped[optional_text]
    status: Mapped[required_text]
    # Для сверки зависших платежей (режим hang у заглушки).
    last_checked_at: Mapped[timestamptz_null]
    created_at: Mapped[created_at]
    updated_at: Mapped[created_at] = mapped_column(onupdate=text("now()"))
