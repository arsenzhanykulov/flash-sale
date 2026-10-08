from enum import StrEnum

from sqlalchemy import CheckConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    Base,
    counter,
    created_at,
    required_text,
    status_in,
    timestamptz_null,
    uuid_pk,
)


class OutboxStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class OutboxMessage(Base):
    """Письма на отправку (ADR-004, инвариант 6).

    Запись создаётся в той же транзакции, что и смена статуса; уникальный
    dedup_key вместе с `INSERT ... ON CONFLICT DO NOTHING` даёт «ровно одно
    письмо на событие».
    """

    __tablename__ = "outbox"
    __table_args__ = (CheckConstraint(status_in("status", OutboxStatus), name="status_known"),)

    id: Mapped[uuid_pk]
    kind: Mapped[required_text]
    dedup_key: Mapped[required_text] = mapped_column(unique=True)
    recipient: Mapped[required_text]
    payload: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[required_text]
    attempts: Mapped[counter]
    created_at: Mapped[created_at]
    sent_at: Mapped[timestamptz_null]
