"""Серверное время.

Инвариант 2: время решает сервер. Единственный источник — now() в Postgres,
чтобы распродажа начиналась и заканчивалась по тем же часам, по которым
работает резерв.
"""

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def get_server_time(session: AsyncSession) -> datetime:
    now = await session.scalar(text("select now()"))
    if now is None:  # now() не бывает NULL, но тип возвращаемого значения — Any
        raise RuntimeError("БД не вернула время")
    return now
