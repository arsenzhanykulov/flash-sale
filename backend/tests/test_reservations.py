"""Резерв брони.

Шаг 2.0: под эндпоинтом наивная реализация с гонкой. Тестов на конкурентность
здесь намеренно нет — они приедут вместе с атомарной версией (шаг 2.1).
Пока проверяем только то, что бронь вообще создаётся.
"""

from datetime import timedelta
from uuid import uuid4

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import MakeSale


async def test_reservation_is_created(
    client: AsyncClient, session: AsyncSession, make_sale: MakeSale
) -> None:
    sale_id = await make_sale(quantity=5)
    login = await client.post(
        "/api/auth/login", json={"email": f"buyer-{uuid4().hex[:12]}@example.com"}
    )
    token = login.json()["access_token"]

    response = await client.post(
        f"/api/sales/{sale_id}/reservations",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["sale_id"] == str(sale_id)
    assert body["status"] == "held"

    await session.rollback()  # читаем то, что закоммитил сервис

    held = await session.scalar(
        text("select held from sales where id = :sale_id"), {"sale_id": sale_id}
    )
    assert held == 1

    # Инвариант 3: бронь живёт 10 минут, срок считается от времени БД.
    reservation = (
        await session.execute(
            text("""
                select status, expires_at - now() as ttl
                from reservations where id = :reservation_id
            """),
            {"reservation_id": body["id"]},
        )
    ).one()
    assert reservation.status == "held"
    assert timedelta(minutes=9) < reservation.ttl <= timedelta(minutes=10)
