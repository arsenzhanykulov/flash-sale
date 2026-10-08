from sqlalchemy import CheckConstraint, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    Base,
    counter,
    created_at,
    currency_code,
    money_minor,
    timestamptz,
    timestamptz_null,
    uuid_fk,
    uuid_pk,
)


class Sale(Base):
    __tablename__ = "sales"
    __table_args__ = (
        CheckConstraint("price_minor > 0", name="price_positive"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("end_at > start_at", name="period_valid"),
        # Инвариант 1, последняя линия защиты от оверсейла: даже при баге в коде
        # БД не даст продать и удержать больше, чем выставлено.
        CheckConstraint(
            "sold >= 0 AND held >= 0 AND sold + held <= quantity",
            name="not_oversold",
        ),
    )

    id: Mapped[uuid_pk]
    product_id: Mapped[uuid_fk] = mapped_column(ForeignKey("products.id"))
    price_minor: Mapped[money_minor]
    currency: Mapped[currency_code]
    quantity: Mapped[int]
    # sold — оплаченные, held — брони в статусах held и paying.
    sold: Mapped[counter]
    held: Mapped[counter]
    start_at: Mapped[timestamptz]
    end_at: Mapped[timestamptz]
    closed_at: Mapped[timestamptz_null]
    created_at: Mapped[created_at]
