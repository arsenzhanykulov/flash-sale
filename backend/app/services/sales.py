"""Чтение распродаж.

Статус и остаток считает Postgres, а не Python: инвариант 2 — время решает
сервер, а остаток обязан быть согласован со счётчиками на тот же момент.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncSession


class SaleStatus(StrEnum):
    UPCOMING = "upcoming"
    ACTIVE = "active"
    ENDED = "ended"


# Границы совпадают с условием резерва из docs/DATA_MODEL.md
# (now() >= start_at AND now() < end_at AND closed_at IS NULL): в момент
# старта распродажа уже active, в момент end_at — уже ended. Иначе UI и резерв
# расходились бы на последней секунде: кнопка активна, а бронь не выдаётся.
SALE_COLUMNS = """
    s.id,
    s.product_id,
    p.name as product_name,
    p.description as product_description,
    p.image_url as product_image_url,
    s.price_minor,
    s.currency,
    s.quantity,
    s.quantity - s.sold - s.held as remaining,
    s.start_at,
    s.end_at,
    case
        when now() < s.start_at then 'upcoming'
        when s.closed_at is not null or now() >= s.end_at then 'ended'
        else 'active'
    end as status,
    now() as server_time
"""

LIST_SALES_SQL = text(f"""
    select {SALE_COLUMNS}
    from sales s
    join products p on p.id = s.product_id
    order by s.start_at, s.id
""")

GET_SALE_SQL = text(f"""
    select {SALE_COLUMNS}
    from sales s
    join products p on p.id = s.product_id
    where s.id = :sale_id
""")


@dataclass(frozen=True)
class SaleView:
    id: UUID
    product_id: UUID
    product_name: str
    product_description: str | None
    product_image_url: str | None
    price_minor: int
    currency: str
    quantity: int
    # quantity - sold - held; CHECK ck_sales_not_oversold гарантирует >= 0.
    remaining: int
    start_at: datetime
    end_at: datetime
    status: SaleStatus
    # Время, на которое посчитан статус: фронт считает таймер по смещению,
    # не доверяя часам браузера (ADR-009).
    server_time: datetime


def to_view(row: Row) -> SaleView:
    return SaleView(**{**row._mapping, "status": SaleStatus(row.status)})


async def list_sales(session: AsyncSession) -> list[SaleView]:
    result = await session.execute(LIST_SALES_SQL)
    return [to_view(row) for row in result]


async def get_sale(session: AsyncSession, sale_id: UUID) -> SaleView | None:
    row = (await session.execute(GET_SALE_SQL, {"sale_id": sale_id})).one_or_none()
    return None if row is None else to_view(row)
