"""GET /api/time отдаёт время БД, а не часы процесса."""

from datetime import datetime

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def test_time_matches_database_now(client: AsyncClient, session: AsyncSession) -> None:
    response = await client.get("/api/time")
    assert response.status_code == 200

    api_now = datetime.fromisoformat(response.json()["now"])
    await session.rollback()  # now() в Postgres — время начала транзакции
    db_now = await session.scalar(text("select now()"))

    assert api_now.tzinfo is not None, "время должно приходить с часовым поясом"
    assert abs((db_now - api_now).total_seconds()) < 5
