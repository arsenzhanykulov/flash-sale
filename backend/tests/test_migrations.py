"""Миграции накатываются с нуля и откатываются обратно."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import run_migrations

EXPECTED_TABLES = {
    "users",
    "products",
    "sales",
    "reservations",
    "orders",
    "payments",
    "outbox",
}

APP_TABLES_SQL = text("""
    select tablename from pg_tables
    where schemaname = 'public' and tablename <> 'alembic_version'
""")


async def app_tables(session: AsyncSession) -> set[str]:
    result = await session.execute(APP_TABLES_SQL)
    return set(result.scalars().all())


async def test_upgrade_downgrade_roundtrip(session: AsyncSession) -> None:
    """downgrade base убирает всю схему, upgrade head возвращает её целиком.

    Тест заканчивается на head, поэтому порядок тестов в прогоне не важен.
    """
    assert await app_tables(session) == EXPECTED_TABLES

    # Сессия держит своё соединение; миграции идут по другому,
    # поэтому закрываем транзакцию, чтобы не ждать блокировок на DROP TABLE.
    await session.rollback()
    await run_migrations("base", downgrade=True)
    assert await app_tables(session) == set()

    await session.rollback()
    await run_migrations("head")
    assert await app_tables(session) == EXPECTED_TABLES
