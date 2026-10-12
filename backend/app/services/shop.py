"""Дашборд магазина: свои распродажи с агрегатами.

Выручка — сумма оплаченных заказов. `pending_payment` и `failed` в неё
не входят: деньги получены только по `paid`.

Всё одним запросом с `LEFT JOIN LATERAL`: агрегаты считаются рядом со строкой
распродажи, без запроса на каждую (ADR-026).
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.sales import SALE_STATUS_CASE, SaleStatus
from app.services.server_time import get_server_time

# Статус считается тем же выражением, что на витрине (ADR-014): иначе магазин
# и покупатель видели бы разные состояния одной распродажи.
SHOP_SALES_SQL = text(f"""
    select
        s.id,
        s.product_id,
        p.name as product_name,
        s.price_minor,
        s.currency,
        s.quantity,
        s.sold,
        s.held,
        s.quantity - s.sold - s.held as remaining,
        {SALE_STATUS_CASE} as status,
        s.start_at,
        s.end_at,
        s.closed_at,
        -- «Не продано» имеет смысл только после закрытия: пока распродажа
        -- идёт, остаток ещё может уйти.
        case when s.closed_at is not null then s.quantity - s.sold end as unsold,
        coalesce(paid.revenue_minor, 0) as revenue_minor,
        coalesce(paid.orders_count, 0) as paid_orders_count,
        now() as server_time
    from sales s
    join products p on p.id = s.product_id
    left join lateral (
        select
            sum(o.amount_minor) as revenue_minor,
            count(*) as orders_count
        from orders o
        where o.sale_id = s.id and o.status = 'paid'
    ) paid on true
    where p.shop_id = :shop_id
    order by s.start_at desc, s.id
""")


@dataclass(frozen=True)
class ShopSaleRow:
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
    # None, пока распродажа не закрыта.
    unsold: int | None
    revenue_minor: int
    paid_orders_count: int


@dataclass(frozen=True)
class ShopSalesView:
    server_time: datetime
    sales: list[ShopSaleRow]


async def list_shop_sales(session: AsyncSession, *, shop_id: UUID) -> ShopSalesView:
    """Распродажи своих товаров. Чужие не видны: фильтр по `products.shop_id`."""
    server_time = await get_server_time(session)
    result = await session.execute(SHOP_SALES_SQL, {"shop_id": shop_id})
    return ShopSalesView(
        server_time=server_time,
        sales=[
            ShopSaleRow(
                **{key: value for key, value in row._mapping.items() if key != "server_time"}
                | {"status": SaleStatus(row.status)}
            )
            for row in result
        ],
    )
