"""Заказы покупателя."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.server_time import get_server_time

MY_ORDERS_SQL = text("""
    select
        o.id,
        o.sale_id,
        p.name as product_name,
        o.amount_minor,
        o.currency,
        o.status,
        o.created_at,
        o.paid_at
    from orders o
    join sales s on s.id = o.sale_id
    join products p on p.id = s.product_id
    where o.user_id = :user_id
    order by o.created_at desc
""")


@dataclass(frozen=True)
class OrderItem:
    id: UUID
    sale_id: UUID
    product_name: str
    amount_minor: int
    currency: str
    status: str
    created_at: datetime
    paid_at: datetime | None


@dataclass(frozen=True)
class MyOrdersView:
    # Одно время на весь ответ: клиент считает по нему относительные даты,
    # не доверяя часам браузера (ADR-009).
    server_time: datetime
    orders: list[OrderItem]


async def list_my_orders(session: AsyncSession, *, user_id: UUID) -> MyOrdersView:
    """Заказы покупателя, свежие сверху. Только свои."""
    server_time = await get_server_time(session)
    result = await session.execute(MY_ORDERS_SQL, {"user_id": user_id})
    return MyOrdersView(
        server_time=server_time,
        orders=[OrderItem(**row._mapping) for row in result],
    )
