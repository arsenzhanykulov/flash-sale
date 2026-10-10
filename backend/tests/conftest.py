"""Общие фикстуры. Тесты идут на реальном Postgres, в отдельной базе.

Рабочая база (`DATABASE_URL`) не трогается: адрес тестовой базы либо задан
в `TEST_DATABASE_URL`, либо выводится из рабочего как `<имя>_test`.
Перед прогоном база пересоздаётся с нуля и схема накатывается миграциями,
после прогона база удаляется.
"""

import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import pytest_asyncio
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import get_settings
from app.core.db import get_engine, get_sessionmaker, reset_engine

# База, к которой подключаемся, чтобы создать и удалить тестовую:
# нельзя выполнить DROP DATABASE, будучи подключённым к ней же.
MAINTENANCE_DB = "postgres"

# Имя базы подставляется в SQL как идентификатор, поэтому допускаем
# только безопасный набор символов.
SAFE_DB_NAME = re.compile(r"\A[A-Za-z0-9_]+\Z")

BACKEND_DIR = Path(__file__).resolve().parent.parent
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"


def database_name(url: str) -> str:
    return urlsplit(url).path.lstrip("/")


def with_database(url: str, db_name: str) -> str:
    return urlunsplit(urlsplit(url)._replace(path=f"/{db_name}"))


def resolve_test_database_url() -> str:
    """Адрес тестовой базы + защита от прогона тестов по рабочей базе."""
    app_url = os.environ.get("DATABASE_URL")
    if not app_url:
        raise RuntimeError(
            "DATABASE_URL не задан. Тесты гоняются на реальном Postgres: "
            "запускай их через `docker compose exec backend pytest -q`."
        )

    test_url = os.environ.get("TEST_DATABASE_URL") or with_database(
        app_url, f"{database_name(app_url)}_test"
    )

    app_db = database_name(app_url)
    test_db = database_name(test_url)

    if not test_db:
        raise RuntimeError(f"в адресе тестовой базы не указано имя базы: {test_url!r}")
    if not test_db.endswith("_test"):
        raise RuntimeError(
            f"имя тестовой базы {test_db!r} должно заканчиваться на '_test' — "
            "защита от удаления рабочей базы."
        )
    if test_db == app_db:
        raise RuntimeError(
            f"тестовая и рабочая база совпадают ({app_db!r}): тесты пересоздают базу, "
            "работать по рабочей нельзя."
        )
    if not SAFE_DB_NAME.match(test_db):
        raise RuntimeError(f"недопустимое имя тестовой базы: {test_db!r}")

    return test_url


def alembic_config(connection: Connection) -> Config:
    """Конфиг Alembic, работающий на уже открытом соединении.

    env.py в этом режиме не создаёт свой engine: asyncio.run() внутри
    работающего event loop вызвать нельзя.
    """
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    config.attributes["connection"] = connection
    return config


async def run_migrations(revision: str, *, downgrade: bool = False) -> None:
    async with get_engine().begin() as conn:
        await conn.run_sync(
            lambda sync_conn: (command.downgrade if downgrade else command.upgrade)(
                alembic_config(sync_conn), revision
            )
        )


@pytest_asyncio.fixture(scope="session", loop_scope="session", autouse=True)
async def test_database() -> AsyncIterator[str]:
    """Создаёт тестовую базу, накатывает миграции и переключает на неё приложение."""
    test_url = resolve_test_database_url()
    test_db = database_name(test_url)
    original_database_url = os.environ["DATABASE_URL"]

    admin_engine = create_async_engine(
        with_database(test_url, MAINTENANCE_DB),
        isolation_level="AUTOCOMMIT",  # CREATE/DROP DATABASE нельзя в транзакции
    )
    try:
        async with admin_engine.connect() as conn:
            # Каждый прогон начинается с чистой базы, без следов предыдущего.
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{test_db}" WITH (FORCE)'))
            await conn.execute(text(f'CREATE DATABASE "{test_db}"'))

        # Приложение читает настройки из окружения — подменяем адрес
        # и сбрасываем кэш настроек вместе с кэшем engine.
        os.environ["DATABASE_URL"] = test_url
        get_settings.cache_clear()
        await reset_engine()

        # Схема накатывается миграциями, а не create_all: тесты проверяют
        # ровно то, что окажется на рабочей базе.
        await run_migrations("head")

        yield test_url

        await reset_engine()
        async with admin_engine.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{test_db}" WITH (FORCE)'))
    finally:
        os.environ["DATABASE_URL"] = original_database_url
        get_settings.cache_clear()
        await admin_engine.dispose()


@pytest_asyncio.fixture(loop_scope="session")
async def client(test_database: str) -> AsyncIterator[AsyncClient]:
    """HTTP-клиент поверх ASGI-приложения.

    Приложение импортируется внутри фикстуры — после того как test_database
    подменила DATABASE_URL.
    """
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http_client:
        yield http_client


@pytest_asyncio.fixture(loop_scope="session")
async def session(test_database: str) -> AsyncIterator[AsyncSession]:
    """Сессия с откатом в конце: тест не оставляет данных после себя."""
    async with get_sessionmaker()() as db_session:
        try:
            yield db_session
        finally:
            await db_session.rollback()


ACTIVE_RESERVATION_STATUSES_SQL = "'held', 'paying', 'paid'"


async def read_counters(session: AsyncSession, sale_id: UUID) -> tuple[int, int, int]:
    """quantity, sold, held по распродаже — из закоммиченных данных."""
    await session.rollback()  # не читаем снимок своей старой транзакции
    row = (
        await session.execute(
            text("select quantity, sold, held from sales where id = :sale_id"),
            {"sale_id": sale_id},
        )
    ).one()
    return row.quantity, row.sold, row.held


async def count_active_reservations(session: AsyncSession, sale_id: UUID) -> int:
    await session.rollback()
    count = await session.scalar(
        text(f"""
            select count(*) from reservations
            where sale_id = :sale_id
              and status in ({ACTIVE_RESERVATION_STATUSES_SQL})
        """),
        {"sale_id": sale_id},
    )
    return count or 0


async def assert_counters_consistent(session: AsyncSession, sale_id: UUID) -> None:
    """ADR-002: sold + held обязаны совпадать с числом занимающих товар броней.

    Счётчики в sales меняются только вместе со статусом брони и в одной
    транзакции с ним, поэтому расхождение означает потерянное обновление.
    """
    quantity, sold, held = await read_counters(session, sale_id)
    active = await count_active_reservations(session, sale_id)

    assert sold + held == active, (
        f"счётчики разошлись с бронями: sold={sold} + held={held} = {sold + held}, "
        f"а активных броней {active} (quantity={quantity})"
    )
    assert sold + held <= quantity, f"оверсейл: sold={sold} + held={held} > quantity={quantity}"


@dataclass(frozen=True)
class SaleFixture:
    """Готовая распродажа на одну единицу товара и покупатель к ней."""

    shop_id: UUID
    buyer_id: UUID
    product_id: UUID
    sale_id: UUID


async def create_shop_with_product(session: AsyncSession) -> tuple[UUID, UUID, UUID]:
    """Магазин, покупатель и товар с уникальными именами.

    Суффикс делает данные каждого теста своими, даже если строки
    останутся в базе до конца прогона.
    """
    suffix = uuid4().hex[:12]

    shop_id = await session.scalar(
        text("insert into users (email, role) values (:email, 'shop') returning id"),
        {"email": f"shop-{suffix}@example.com"},
    )
    buyer_id = await session.scalar(
        text("insert into users (email, role) values (:email, 'buyer') returning id"),
        {"email": f"buyer-{suffix}@example.com"},
    )
    product_id = await session.scalar(
        text("insert into products (shop_id, name) values (:shop_id, :name) returning id"),
        {"shop_id": shop_id, "name": f"Товар {suffix}"},
    )
    return shop_id, buyer_id, product_id


@pytest_asyncio.fixture(loop_scope="session")
async def sale(session: AsyncSession) -> SaleFixture:
    shop_id, buyer_id, product_id = await create_shop_with_product(session)
    sale_id = await session.scalar(
        text("""
            insert into sales (product_id, price_minor, currency, quantity, start_at, end_at)
            values (:product_id, 100000, 'KGS', 1, now() - interval '1 minute',
                    now() + interval '1 hour')
            returning id
        """),
        {"product_id": product_id},
    )

    return SaleFixture(
        shop_id=shop_id,
        buyer_id=buyer_id,
        product_id=product_id,
        sale_id=sale_id,
    )


# Фабрика распродаж с произвольным окном. Окно задаётся SQL-выражениями
# относительно now() в БД: время должно быть серверным (инвариант 2), а
# подставлять datetime из Python означало бы сравнивать разные часы.
# Интерполяция в SQL безопасна: значения приходят только из кода тестов.
MakeSale = Callable[..., Awaitable[UUID]]


@pytest_asyncio.fixture(loop_scope="session")
async def make_sale(session: AsyncSession) -> MakeSale:
    async def _make_sale(
        *,
        start_at: str = "now()",
        end_at: str = "now() + interval '1 hour'",
        quantity: int = 5,
        sold: int = 0,
        held: int = 0,
        closed: bool = False,
    ) -> UUID:
        _, _, product_id = await create_shop_with_product(session)
        sale_id = await session.scalar(
            text(f"""
                insert into sales (
                    product_id, price_minor, currency, quantity, sold, held,
                    start_at, end_at, closed_at
                )
                values (
                    :product_id, 100000, 'KGS', :quantity, :sold, :held,
                    {start_at}, {end_at}, {"now()" if closed else "null"}
                )
                returning id
            """),
            {"product_id": product_id, "quantity": quantity, "sold": sold, "held": held},
        )
        # Коммит обязателен: запросы к API идут через отдельную сессию
        # и незакоммиченных строк не увидят.
        await session.commit()
        return sale_id

    return _make_sale
