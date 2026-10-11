from enum import StrEnum

from sqlalchemy import CheckConstraint, ForeignKey, Index, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    Base,
    counter,
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
    __table_args__ = (
        CheckConstraint(status_in("status", PaymentStatus), name="status_known"),
        CheckConstraint("reconcile_attempts >= 0", name="reconcile_attempts_not_negative"),
        # Сверка выбирает только незавершённые платежи, поэтому индекс
        # частичный. NULLS FIRST: ещё не проверявшиеся платежи идут первыми,
        # и порядок индекса совпадает с ORDER BY в запросе сверки.
        Index(
            "ix_payments_pending_last_checked_at",
            text("last_checked_at nulls first"),
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[uuid_pk]
    # Инвариант 5: один платёж на заказ.
    order_id: Mapped[uuid_fk] = mapped_column(ForeignKey("orders.id"), unique=True)
    # Передаётся в заглушку: повторный клик не создаёт второй платёж.
    idempotency_key: Mapped[required_text] = mapped_column(unique=True)
    provider_payment_id: Mapped[optional_text]
    status: Mapped[required_text]
    # Для сверки зависших платежей (режим hang у заглушки).
    last_checked_at: Mapped[timestamptz_null]
    # Сколько раз сверка уже спрашивала провайдера. По нему считается
    # нарастающий интервал; хранится в БД, чтобы перезапуск backend
    # не сбрасывал прогресс (ADR-023).
    reconcile_attempts: Mapped[counter]
    created_at: Mapped[created_at]
    updated_at: Mapped[created_at] = mapped_column(onupdate=text("now()"))
