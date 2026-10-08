"""Alembic: async-конфигурация.

Адрес БД берётся из настроек приложения (переменные окружения), в alembic.ini
его нет — один источник правды и никаких секретов в файлах.

Поддерживаются два режима:
- обычный (`alembic upgrade head`) — создаётся свой AsyncEngine;
- на уже открытом соединении через `config.attributes["connection"]` — так
  миграции прогоняет тестовая фикстура, внутри которой нельзя вызвать
  asyncio.run(), поскольку event loop уже работает.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Без этих двух флагов autogenerate молча пропускает изменения
        # типов колонок и server_default.
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    """Режим --sql: генерация SQL без подключения к БД."""
    context.configure(
        url=get_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(get_settings().database_url, poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    existing_connection = config.attributes.get("connection")
    if existing_connection is not None:
        do_run_migrations(existing_connection)
    else:
        asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
