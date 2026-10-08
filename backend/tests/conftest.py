"""Общие фикстуры. Тесты идут на реальном Postgres, в отдельной базе.

Рабочая база (`DATABASE_URL`) не трогается: адрес тестовой базы либо задан
в `TEST_DATABASE_URL`, либо выводится из рабочего как `<имя>_test`.
Перед прогоном база пересоздаётся с нуля, после прогона удаляется.
"""

import os
import re
from collections.abc import AsyncIterator
from urllib.parse import urlsplit, urlunsplit

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.core.db import reset_engine

# База, к которой подключаемся, чтобы создать и удалить тестовую:
# нельзя выполнить DROP DATABASE, будучи подключённым к ней же.
MAINTENANCE_DB = "postgres"

# Имя базы подставляется в SQL как идентификатор, поэтому допускаем
# только безопасный набор символов.
SAFE_DB_NAME = re.compile(r"\A[A-Za-z0-9_]+\Z")


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


@pytest_asyncio.fixture(scope="session", loop_scope="session", autouse=True)
async def test_database() -> AsyncIterator[str]:
    """Создаёт тестовую базу и переключает на неё приложение."""
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
