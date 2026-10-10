from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.services.sales import SaleStatus


class SaleSnapshotMessage(BaseModel):
    """Состояние распродажи, уходящее по WebSocket.

    Товар и цену не повторяем: они неизменны, клиент получил их по HTTP.
    """

    model_config = ConfigDict(from_attributes=True)

    sale_id: UUID
    quantity: int
    sold: int
    held: int
    remaining: int
    status: SaleStatus
    # Время, на которое посчитан снимок: клиент считает таймер по смещению
    # от него, а не по часам браузера (ADR-009).
    server_time: datetime
