from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ReservationResponse(BaseModel):
    # from_attributes: собирается из ReservationView, который отдаёт сервис.
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    sale_id: UUID
    status: str
    # Инвариант 3: бронь в held живёт 10 минут от создания.
    expires_at: datetime
    created_at: datetime
