from httpx import AsyncClient
from pytest import MonkeyPatch
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import get_sessionmaker, reset_engine
from tests.conftest import database_name

# Порт 1 — соединение сразу отклоняется, тест не ждёт таймаута.
UNREACHABLE_DATABASE_URL = "postgresql+asyncpg://nobody:nobody@127.0.0.1:1/nonexistent"


async def test_health_ok_on_test_database(client: AsyncClient, test_database: str) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}

    # Тест обязан работать с отдельной базой, а не с рабочей.
    async with get_sessionmaker()() as session:
        current_database = await session.scalar(text("select current_database()"))

    assert current_database == database_name(test_database)
    assert current_database.endswith("_test")


async def test_health_returns_503_when_database_unreachable(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", UNREACHABLE_DATABASE_URL)
    get_settings.cache_clear()
    await reset_engine()
    try:
        response = await client.get("/health")
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
        await reset_engine()

    assert response.status_code == 503
    assert response.json() == {"status": "error", "database": "unavailable"}
