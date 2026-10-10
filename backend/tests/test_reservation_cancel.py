"""Отмена брони и список своих активных броней.

Отмена — тоже условный UPDATE: решение «можно ли отменить» принимает БД
тем же оператором, что и записывает. Иначе две одновременные отмены
уменьшили бы held дважды и единица товара пропала бы.
"""

import asyncio
from datetime import datetime
from uuid import UUID, uuid4

from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import MakeSale, assert_counters_consistent, read_counters

CONCURRENT_CANCELS = 10


async def login(client: AsyncClient) -> str:
    response = await client.post(
        "/api/auth/login", json={"email": f"buyer-{uuid4().hex[:12]}@example.com"}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def reserve(client: AsyncClient, sale_id: UUID, token: str) -> Response:
    return await client.post(f"/api/sales/{sale_id}/reservations", headers=auth(token))


async def cancel(client: AsyncClient, reservation_id: str, token: str) -> Response:
    return await client.delete(f"/api/reservations/{reservation_id}", headers=auth(token))


async def reservation_status(session: AsyncSession, reservation_id: str) -> str | None:
    await session.rollback()
    return await session.scalar(
        text("select status from reservations where id = :reservation_id"),
        {"reservation_id": reservation_id},
    )


async def remaining(client: AsyncClient, sale_id: UUID) -> int:
    response = await client.get(f"/api/sales/{sale_id}")
    assert response.status_code == 200
    return response.json()["remaining"]


async def test_cancel_returns_the_unit(
    client: AsyncClient, session: AsyncSession, make_sale: MakeSale
) -> None:
    sale_id = await make_sale(quantity=3)
    token = await login(client)

    reservation = (await reserve(client, sale_id, token)).json()
    assert await remaining(client, sale_id) == 2

    response = await cancel(client, reservation["id"], token)

    assert response.status_code == 204
    assert response.content == b""
    assert await remaining(client, sale_id) == 3
    assert await reservation_status(session, reservation["id"]) == "cancelled"
    _, sold, held = await read_counters(session, sale_id)
    assert (sold, held) == (0, 0)
    await assert_counters_consistent(session, sale_id)


async def test_second_cancel_is_idempotent(
    client: AsyncClient, session: AsyncSession, make_sale: MakeSale
) -> None:
    sale_id = await make_sale(quantity=1)
    token = await login(client)
    reservation = (await reserve(client, sale_id, token)).json()

    first = await cancel(client, reservation["id"], token)
    second = await cancel(client, reservation["id"], token)

    assert (first.status_code, second.status_code) == (204, 204)
    _, sold, held = await read_counters(session, sale_id)
    assert held == 0, "повторная отмена уменьшила held второй раз"
    assert (sold, held) == (0, 0)
    await assert_counters_consistent(session, sale_id)


async def test_cancelling_someone_elses_reservation_is_404(
    client: AsyncClient, session: AsyncSession, make_sale: MakeSale
) -> None:
    """Чужую бронь не отличаем от несуществующей — иначе перебором можно
    выяснить, какие брони существуют."""
    sale_id = await make_sale(quantity=3)
    owner_token = await login(client)
    stranger_token = await login(client)
    reservation = (await reserve(client, sale_id, owner_token)).json()

    response = await cancel(client, reservation["id"], stranger_token)

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "reservation_not_found"
    assert await reservation_status(session, reservation["id"]) == "held"
    _, sold, held = await read_counters(session, sale_id)
    assert (sold, held) == (0, 1), "чужая отмена тронула счётчики"
    await assert_counters_consistent(session, sale_id)


async def test_unknown_reservation_is_404(client: AsyncClient) -> None:
    token = await login(client)

    response = await cancel(client, str(uuid4()), token)

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "reservation_not_found"


async def test_cancel_requires_login(client: AsyncClient) -> None:
    response = await client.delete(f"/api/reservations/{uuid4()}")

    assert response.status_code == 401


async def test_paying_reservation_cannot_be_cancelled(
    client: AsyncClient, session: AsyncSession, make_sale: MakeSale
) -> None:
    """Инвариант 4: начатая оплата доводится до конца."""
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    reservation = (await reserve(client, sale_id, token)).json()

    await session.execute(
        text("update reservations set status = 'paying' where id = :reservation_id"),
        {"reservation_id": reservation["id"]},
    )
    await session.commit()

    response = await cancel(client, reservation["id"], token)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "not_cancellable"
    assert await reservation_status(session, reservation["id"]) == "paying"
    _, sold, held = await read_counters(session, sale_id)
    assert (sold, held) == (0, 1)
    await assert_counters_consistent(session, sale_id)


async def test_expired_reservation_cancel_is_noop(
    client: AsyncClient, session: AsyncSession, make_sale: MakeSale
) -> None:
    """Свипер уже вернул единицу — отмена отвечает 204 и ничего не меняет."""
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    reservation = (await reserve(client, sale_id, token)).json()

    await session.execute(
        text("update reservations set status = 'expired' where id = :reservation_id"),
        {"reservation_id": reservation["id"]},
    )
    await session.execute(
        text("update sales set held = held - 1 where id = :sale_id"), {"sale_id": sale_id}
    )
    await session.commit()

    response = await cancel(client, reservation["id"], token)

    assert response.status_code == 204
    _, sold, held = await read_counters(session, sale_id)
    assert (sold, held) == (0, 0), "отмена истёкшей брони тронула счётчики"
    await assert_counters_consistent(session, sale_id)


async def test_buyer_can_reserve_again_after_cancel(
    client: AsyncClient, session: AsyncSession, make_sale: MakeSale
) -> None:
    sale_id = await make_sale(quantity=1)
    token = await login(client)
    first = (await reserve(client, sale_id, token)).json()
    assert (await cancel(client, first["id"], token)).status_code == 204

    second = await reserve(client, sale_id, token)

    assert second.status_code == 201, second.text
    assert second.json()["id"] != first["id"]
    _, sold, held = await read_counters(session, sale_id)
    assert (sold, held) == (0, 1)
    await assert_counters_consistent(session, sale_id)


async def test_concurrent_cancels_release_the_unit_once(
    client: AsyncClient, session: AsyncSession, make_sale: MakeSale
) -> None:
    """Десять одновременных отмен одной брони уменьшают held ровно на 1.

    Старт синхронизирован барьером, соединение у каждого запроса своё.
    """
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    reservation = (await reserve(client, sale_id, token)).json()
    _, _, held_before = await read_counters(session, sale_id)
    assert held_before == 1

    barrier = asyncio.Barrier(CONCURRENT_CANCELS)

    async def attempt() -> Response:
        await barrier.wait()
        return await cancel(client, reservation["id"], token)

    responses = list(await asyncio.gather(*(attempt() for _ in range(CONCURRENT_CANCELS))))

    codes = sorted(response.status_code for response in responses)
    assert codes == [204] * CONCURRENT_CANCELS, f"коды ответов: {codes}"

    _, sold, held = await read_counters(session, sale_id)
    assert (sold, held) == (0, 0), f"held уменьшился не на 1: было 1, стало {held}"
    assert await reservation_status(session, reservation["id"]) == "cancelled"
    await assert_counters_consistent(session, sale_id)


async def test_me_shows_only_own_active_reservations(
    client: AsyncClient, session: AsyncSession, make_sale: MakeSale
) -> None:
    held_sale = await make_sale(quantity=3)
    paying_sale = await make_sale(quantity=3)
    cancelled_sale = await make_sale(quantity=3)

    token = await login(client)
    stranger_token = await login(client)

    held = (await reserve(client, held_sale, token)).json()
    paying = (await reserve(client, paying_sale, token)).json()
    cancelled = (await reserve(client, cancelled_sale, token)).json()
    stranger = (await reserve(client, held_sale, stranger_token)).json()

    await session.execute(
        text("update reservations set status = 'paying' where id = :reservation_id"),
        {"reservation_id": paying["id"]},
    )
    await session.commit()
    assert (await cancel(client, cancelled["id"], token)).status_code == 204

    response = await client.get("/api/reservations/me", headers=auth(token))

    assert response.status_code == 200
    body = response.json()
    returned = {item["id"]: item["status"] for item in body["reservations"]}

    assert returned == {held["id"]: "held", paying["id"]: "paying"}
    assert cancelled["id"] not in returned, "отменённая бронь попала в список"
    assert stranger["id"] not in returned, "чужая бронь попала в список"

    # Время сервера есть и совпадает с now() в БД.
    await session.rollback()
    db_now = await session.scalar(text("select now()"))

    server_time = datetime.fromisoformat(body["server_time"])
    assert abs((db_now - server_time).total_seconds()) < 5

    for sale_id in (held_sale, paying_sale, cancelled_sale):
        await assert_counters_consistent(session, sale_id)


async def test_me_requires_login(client: AsyncClient) -> None:
    response = await client.get("/api/reservations/me")

    assert response.status_code == 401


async def test_me_is_empty_for_new_buyer(client: AsyncClient) -> None:
    token = await login(client)

    response = await client.get("/api/reservations/me", headers=auth(token))

    assert response.status_code == 200
    body = response.json()
    assert body["reservations"] == []
    assert body["server_time"], "server_time нужен и для пустого списка"
