"""Демо-данные: магазин, товар, два покупателя и распродажа.

    python -m app.scripts.seed [--quantity 5] [--start-in 2] [--duration 30]

Повторный запуск не ломается и не плодит сущности:
- пользователи и товар находятся по своим постоянным email и названию;
- распродажа переиспользуется только своя — созданная по seed-товару — и только
  если по ней ещё нет броней. Иначе создаётся новая: обнулять sold/held у
  распродажи с бронями нельзя, счётчики разъехались бы со строками
  в reservations (ADR-002).

Окно считается от now() в БД (инвариант 2), а не от часов машины.
"""

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_sessionmaker, reset_engine

SHOP_EMAIL = "shop@example.com"
BUYER_EMAILS = ("buyer1@example.com", "buyer2@example.com")

PRODUCT_NAME = "Беспроводные наушники Flash One"
PRODUCT_DESCRIPTION = "Активное шумоподавление, 30 часов автономной работы."
PRODUCT_IMAGE_URL = "https://picsum.photos/seed/flash-one/600/400"

PRICE_MINOR = 499_000
CURRENCY = "KGS"

DEFAULT_QUANTITY = 5
DEFAULT_START_IN_MINUTES = 2
DEFAULT_DURATION_MINUTES = 30

# DO UPDATE SET role: эти пользователи принадлежат seed-у, и если магазин
# успели создать входом (он создаёт buyer), роль нужно вернуть на место.
UPSERT_USER_SQL = text("""
    insert into users (email, role)
    values (:email, :role)
    on conflict (email) do update set role = excluded.role
    returning id
""")

SELECT_PRODUCT_SQL = text("""
    select id from products
    where shop_id = :shop_id and name = :name
    limit 1
""")

INSERT_PRODUCT_SQL = text("""
    insert into products (shop_id, name, description, image_url)
    values (:shop_id, :name, :description, :image_url)
    returning id
""")

# Только распродажа seed-товара и только без броней.
SELECT_REUSABLE_SALE_SQL = text("""
    select s.id from sales s
    where s.product_id = :product_id
      and not exists (select 1 from reservations r where r.sale_id = s.id)
    order by s.created_at desc
    limit 1
""")

UPDATE_SALE_SQL = text("""
    update sales set
        price_minor = :price_minor,
        currency = :currency,
        quantity = :quantity,
        sold = 0,
        held = 0,
        closed_at = null,
        start_at = now() + make_interval(mins => :start_in),
        end_at = now() + make_interval(mins => :end_in)
    where id = :sale_id
    returning id, start_at, end_at
""")

INSERT_SALE_SQL = text("""
    insert into sales (product_id, price_minor, currency, quantity, start_at, end_at)
    values (
        :product_id, :price_minor, :currency, :quantity,
        now() + make_interval(mins => :start_in),
        now() + make_interval(mins => :end_in)
    )
    returning id, start_at, end_at
""")


@dataclass(frozen=True)
class SeedResult:
    shop_id: UUID
    buyer_ids: tuple[UUID, ...]
    product_id: UUID
    sale_id: UUID
    start_at: datetime
    end_at: datetime
    quantity: int
    sale_reused: bool


async def seed(
    session: AsyncSession,
    *,
    quantity: int = DEFAULT_QUANTITY,
    start_in: int = DEFAULT_START_IN_MINUTES,
    duration: int = DEFAULT_DURATION_MINUTES,
) -> SeedResult:
    shop_id = await session.scalar(UPSERT_USER_SQL, {"email": SHOP_EMAIL, "role": "shop"})
    buyer_ids = tuple(
        [
            await session.scalar(UPSERT_USER_SQL, {"email": email, "role": "buyer"})
            for email in BUYER_EMAILS
        ]
    )

    product_params = {"shop_id": shop_id, "name": PRODUCT_NAME}
    product_id = await session.scalar(SELECT_PRODUCT_SQL, product_params)
    if product_id is None:
        product_id = await session.scalar(
            INSERT_PRODUCT_SQL,
            product_params | {"description": PRODUCT_DESCRIPTION, "image_url": PRODUCT_IMAGE_URL},
        )

    sale_params = {
        "product_id": product_id,
        "price_minor": PRICE_MINOR,
        "currency": CURRENCY,
        "quantity": quantity,
        "start_in": start_in,
        "end_in": start_in + duration,
    }
    reusable_sale_id = await session.scalar(SELECT_REUSABLE_SALE_SQL, {"product_id": product_id})
    if reusable_sale_id is None:
        sale_row = (await session.execute(INSERT_SALE_SQL, sale_params)).one()
    else:
        sale_row = (
            await session.execute(UPDATE_SALE_SQL, sale_params | {"sale_id": reusable_sale_id})
        ).one()

    await session.commit()

    return SeedResult(
        shop_id=shop_id,
        buyer_ids=buyer_ids,
        product_id=product_id,
        sale_id=sale_row.id,
        start_at=sale_row.start_at,
        end_at=sale_row.end_at,
        quantity=quantity,
        sale_reused=reusable_sale_id is not None,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.seed",
        description="Создать демо-данные для флэш-распродажи.",
    )
    parser.add_argument(
        "--quantity",
        type=int,
        default=DEFAULT_QUANTITY,
        help=f"сколько единиц товара выставить (по умолчанию {DEFAULT_QUANTITY})",
    )
    parser.add_argument(
        "--start-in",
        type=int,
        default=DEFAULT_START_IN_MINUTES,
        metavar="MINUTES",
        help=(
            "через сколько минут старт; отрицательное значение — распродажа уже идёт "
            f"(по умолчанию {DEFAULT_START_IN_MINUTES})"
        ),
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=DEFAULT_DURATION_MINUTES,
        metavar="MINUTES",
        help=f"длительность в минутах (по умолчанию {DEFAULT_DURATION_MINUTES})",
    )

    args = parser.parse_args(argv)
    if args.quantity < 1:
        parser.error("--quantity должен быть не меньше 1")
    if args.duration < 1:
        # CHECK ck_sales_period_valid требует end_at > start_at.
        parser.error("--duration должен быть не меньше 1")
    return args


async def run(args: argparse.Namespace) -> SeedResult:
    try:
        async with get_sessionmaker()() as session:
            return await seed(
                session,
                quantity=args.quantity,
                start_in=args.start_in,
                duration=args.duration,
            )
    finally:
        await reset_engine()


def main() -> None:
    args = parse_args()
    result = asyncio.run(run(args))

    print("Демо-данные готовы.")
    print(f"  магазин:     {SHOP_EMAIL}")
    print(f"  покупатели:  {', '.join(BUYER_EMAILS)}")
    print(f"  товар:       {PRODUCT_NAME} ({result.product_id})")
    print(f"  распродажа:  {result.sale_id} — {'обновлена' if result.sale_reused else 'создана'}")
    print(f"  количество:  {result.quantity}")
    print(f"  старт:       {result.start_at:%Y-%m-%d %H:%M:%S %Z}")
    print(f"  конец:       {result.end_at:%Y-%m-%d %H:%M:%S %Z}")
    print()
    print("Войти: POST /api/auth/login с любым из email выше.")


if __name__ == "__main__":
    main()
