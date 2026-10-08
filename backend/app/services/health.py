"""Проверка живости зависимостей."""

import logging

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.db import get_sessionmaker

logger = logging.getLogger(__name__)


async def ping_db() -> bool:
    """True, если БД отвечает на `select 1`.

    Сессия открывается здесь, а не приходит через Depends: ошибка подключения
    внутри зависимости вылетела бы до тела эндпоинта и FastAPI вернул бы 500,
    а нам нужен 503.
    """
    try:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            await session.execute(text("select 1"))
    except (SQLAlchemyError, OSError):
        logger.warning("health: база данных недоступна", exc_info=True)
        return False
    return True
