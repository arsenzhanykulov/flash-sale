from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.services.sales import SaleStatus


class ShopSaleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    product_id: UUID
    product_name: str
    price_minor: int
    currency: str
    quantity: int
    sold: int
    held: int
    remaining: int
    status: SaleStatus
    start_at: datetime
    end_at: datetime
    closed_at: datetime | None
    # Заполняется только после закрытия распродажи.
    unsold: int | None
    # Сумма оплаченных заказов: pending_payment и failed не считаются.
    revenue_minor: int
    paid_orders_count: int


class ShopSalesResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    server_time: datetime
    sales: list[ShopSaleResponse]
