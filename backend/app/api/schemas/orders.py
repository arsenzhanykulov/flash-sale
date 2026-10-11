from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class OrderItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    sale_id: UUID
    product_name: str
    amount_minor: int
    currency: str
    status: str
    created_at: datetime
    paid_at: datetime | None


class MyOrdersResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    server_time: datetime
    orders: list[OrderItemResponse]
