"""Модели SQLAlchemy по docs/DATA_MODEL.md.

Все модели импортируются здесь: Alembic autogenerate видит таблицы только
через Base.metadata, а оно наполняется при импорте модулей.
"""

from app.models.base import Base
from app.models.order import Order, OrderStatus
from app.models.outbox import OutboxMessage, OutboxStatus
from app.models.payment import Payment, PaymentStatus
from app.models.product import Product
from app.models.reservation import (
    ACTIVE_RESERVATION_STATUSES,
    Reservation,
    ReservationStatus,
)
from app.models.sale import Sale
from app.models.user import User, UserRole

__all__ = [
    "ACTIVE_RESERVATION_STATUSES",
    "Base",
    "Order",
    "OrderStatus",
    "OutboxMessage",
    "OutboxStatus",
    "Payment",
    "PaymentStatus",
    "Product",
    "Reservation",
    "ReservationStatus",
    "Sale",
    "User",
    "UserRole",
]
