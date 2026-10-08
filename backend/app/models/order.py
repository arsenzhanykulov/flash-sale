from enum import StrEnum

from sqlalchemy import CheckConstraint, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    Base,
    created_at,
    currency_code,
    money_minor,
    required_text,
    status_in,
    timestamptz_null,
    uuid_fk,
    uuid_pk,
)


class OrderStatus(StrEnum):
    PENDING_PAYMENT = "pending_payment"
    PAID = "paid"
    FAILED = "failed"


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint(status_in("status", OrderStatus), name="status_known"),
        CheckConstraint("amount_minor > 0", name="amount_positive"),
    )

    id: Mapped[uuid_pk]
    # Инвариант 5: один заказ на бронь.
    reservation_id: Mapped[uuid_fk] = mapped_column(ForeignKey("reservations.id"), unique=True)
    user_id: Mapped[uuid_fk] = mapped_column(ForeignKey("users.id"))
    sale_id: Mapped[uuid_fk] = mapped_column(ForeignKey("sales.id"))
    # Сумма фиксируется при создании заказа: позднее изменение цены распродажи
    # на уже созданный заказ не влияет.
    amount_minor: Mapped[money_minor]
    currency: Mapped[currency_code]
    status: Mapped[required_text]
    created_at: Mapped[created_at]
    paid_at: Mapped[timestamptz_null]
