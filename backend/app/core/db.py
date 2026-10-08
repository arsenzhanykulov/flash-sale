"""Подключение к БД: async engine и фабрика сессий.

Engine создаётся лениво, при первом обращении, а не на старте приложения:
так тесты могут поднять ASGI-приложение без прогона lifespan, а /health
остаётся честным — пока engine не создан, соединения ещё нет.
"""

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        echo=settings.db_echo,
        # Иначе /health мог бы ответить ok по мёртвому соединению из пула.
        pool_pre_ping=True,
    )


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def reset_engine() -> None:
    """Закрыть пул соединений и сбросить кэш engine и фабрики сессий.

    Нужно при остановке приложения и в тестах — после подмены DATABASE_URL
    старый engine продолжал бы держать старый адрес.
    """
    if get_engine.cache_info().currsize:
        await get_engine().dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


async def get_session() -> AsyncIterator[AsyncSession]:
    """Зависимость FastAPI: сессия на один запрос."""
    async with get_sessionmaker()() as session:
        yield session
