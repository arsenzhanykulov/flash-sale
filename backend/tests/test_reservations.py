"""Резерв брони: успешный путь и все коды отказа.

Конкурентные сценарии — в test_reservation_race.py.
"""

from datetime import timedelta
from uuid import uuid4

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import MakeSale, assert_counters_consistent, read_counters


async def login(client: AsyncClient) -> str:
    response = await client.post(
        "/api/auth/login", json={"email": f"buyer-{uuid4().hex[:12]}@example.com"}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def reserve(client: AsyncClient, sale_id, token: str):
    return await client.post(f"/api/sales/{sale_id}/reservations", headers=auth(token))


async def test_reservation_is_created(
    client: AsyncClient, session: AsyncSession, make_sale: MakeSale
) -> None:
    sale_id = await make_sale(quantity=5)
    token = await login(client)

    response = await reserve(client, sale_id, token)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["sale_id"] == str(sale_id)
    assert body["status"] == "held"

    _, sold, held = await read_counters(session, sale_id)
    assert (sold, held) == (0, 1)
    await assert_counters_consistent(session, sale_id)

    # Инвариант 3: срок брони считается от времени БД.
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


async def test_reservation_requires_login(client: AsyncClient, make_sale: MakeSale) -> None:
    sale_id = await make_sale()

    response = await client.post(f"/api/sales/{sale_id}/reservations")

    assert response.status_code == 401


async def test_unknown_sale_is_404(client: AsyncClient) -> None:
    token = await login(client)

    response = await reserve(client, uuid4(), token)

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "sale_not_found"


async def test_sale_not_started_is_refused(
    client: AsyncClient, session: AsyncSession, make_sale: MakeSale
) -> None:
    sale_id = await make_sale(
        start_at="now() + interval '1 hour'", end_at="now() + interval '2 hours'"
    )
    token = await login(client)

    response = await reserve(client, sale_id, token)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "not_started"
    # Отказ не должен оставлять следов.
    _, sold, held = await read_counters(session, sale_id)
    assert (sold, held) == (0, 0)


async def test_ended_sale_is_refused(
    client: AsyncClient, session: AsyncSession, make_sale: MakeSale
) -> None:
    sale_id = await make_sale(
        start_at="now() - interval '2 hours'", end_at="now() - interval '1 hour'"
    )
    token = await login(client)

    response = await reserve(client, sale_id, token)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ended"
    _, sold, held = await read_counters(session, sale_id)
    assert (sold, held) == (0, 0)


async def test_closed_sale_is_refused(client: AsyncClient, make_sale: MakeSale) -> None:
    """Закрытая воркером распродажа — тоже ended, хотя окно ещё не истекло."""
    sale_id = await make_sale(closed=True)
    token = await login(client)

    response = await reserve(client, sale_id, token)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ended"


async def test_sold_out_sale_is_refused(
    client: AsyncClient, session: AsyncSession, make_sale: MakeSale
) -> None:
    sale_id = await make_sale(quantity=1, sold=1)
    token = await login(client)

    response = await reserve(client, sale_id, token)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "sold_out"
    _, sold, held = await read_counters(session, sale_id)
    assert (sold, held) == (1, 0)


async def test_second_reservation_by_same_buyer_is_refused(
    client: AsyncClient, session: AsyncSession, make_sale: MakeSale
) -> None:
    """ADR-008: одна активная бронь на покупателя.

    Главное здесь — что отказ откатывает транзакцию целиком: held не остаётся
    увеличенным, иначе единица товара пропала бы впустую.
    """
    sale_id = await make_sale(quantity=5)
    token = await login(client)

    first = await reserve(client, sale_id, token)
    assert first.status_code == 201

    second = await reserve(client, sale_id, token)

    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "already_reserved"

    _, sold, held = await read_counters(session, sale_id)
    assert (sold, held) == (0, 1), "отказ оставил held увеличенным"
    await assert_counters_consistent(session, sale_id)


async def test_buyer_can_reserve_again_after_previous_expired(
    client: AsyncClient, session: AsyncSession, make_sale: MakeSale
) -> None:
    """Частичный индекс не видит завершённые брони — место освобождается."""
    sale_id = await make_sale(quantity=5)
    token = await login(client)

    first = await reserve(client, sale_id, token)
    assert first.status_code == 201

    # Имитируем работу свипера: бронь истекла, единица вернулась.
    await session.execute(
        text("""
            update reservations set status = 'expired' where id = :reservation_id
        """),
        {"reservation_id": first.json()["id"]},
    )
    await session.execute(
        text("update sales set held = held - 1 where id = :sale_id"), {"sale_id": sale_id}
    )
    await session.commit()

    second = await reserve(client, sale_id, token)

    assert second.status_code == 201, second.text
    await assert_counters_consistent(session, sale_id)
