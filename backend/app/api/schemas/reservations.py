from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.services.reservations import ReserveRefusal


class ReservationResponse(BaseModel):
    # from_attributes: собирается из ReservationView, который отдаёт сервис.
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    sale_id: UUID
    status: str
    # Инвариант 3: бронь живёт RESERVATION_TTL_SECONDS от создания.
    expires_at: datetime
    created_at: datetime


class ReserveErrorDetail(BaseModel):
    """Причина отказа.

    Четыре разные ситуации отдают один и тот же 409, поэтому фронту нужен
    машинный код: ветвиться по тексту сообщения нельзя.
    """

    code: ReserveRefusal
    message: str


class ReserveErrorResponse(BaseModel):
    detail: ReserveErrorDetail
