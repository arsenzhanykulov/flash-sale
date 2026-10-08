from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.services.sales import SaleStatus


class SaleResponse(BaseModel):
    # from_attributes: собирается из SaleView, который отдаёт сервис.
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    product_id: UUID
    product_name: str
    product_description: str | None
    product_image_url: str | None
    price_minor: int
    currency: str
    quantity: int
    # quantity - sold - held, посчитан в БД на server_time.
    remaining: int
    start_at: datetime
    end_at: datetime
    status: SaleStatus
    # Время, на которое посчитан статус: фронт считает таймер по смещению,
    # не доверяя часам браузера (ADR-009).
    server_time: datetime
