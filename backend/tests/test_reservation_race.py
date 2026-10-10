"""Конкурентная борьба за последнюю единицу товара.

Инвариант 1 из CLAUDE.md и ADR-002: продано + удержано никогда не больше
quantity, а резерв делается только атомарным условным UPDATE.

Старт запросов синхронизирован барьером, соединение у каждого покупателя
своё: сессия создаётся зависимостью на каждый HTTP-запрос. То есть это
настоящая конкурентность на стороне Postgres, а не последовательные вызовы.
"""

import asyncio
from uuid import UUID, uuid4

from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import (
    MakeSale,
    assert_counters_consistent,
    count_active_reservations,
    read_counters,
)

# Покупателей заметно больше, чем товара: одна единица на всех.
RACE_BUYERS = 10


async def login_buyers(client: AsyncClient, count: int) -> list[str]:
    tokens = []
    for _ in range(count):
        response = await client.post(
            "/api/auth/login", json={"email": f"racer-{uuid4().hex[:12]}@example.com"}
        )
        assert response.status_code == 200, response.text
        tokens.append(response.json()["access_token"])
    return tokens


async def reserve_simultaneously(
    client: AsyncClient, sale_id: UUID, tokens: list[str]
) -> list[Response]:
    """Все покупатели жмут «Купить» в один момент."""
    barrier = asyncio.Barrier(len(tokens))

    async def attempt(token: str) -> Response:
        await barrier.wait()  # синхронный старт: никто не уходит вперёд
        return await client.post(
            f"/api/sales/{sale_id}/reservations",
            headers={"Authorization": f"Bearer {token}"},
        )

    return list(await asyncio.gather(*(attempt(token) for token in tokens)))


async def test_only_one_buyer_gets_the_last_unit(
    client: AsyncClient, session: AsyncSession, make_sale: MakeSale
) -> None:
    sale_id = await make_sale(quantity=1)
    tokens = await login_buyers(client, RACE_BUYERS)

    responses = await reserve_simultaneously(client, sale_id, tokens)

    created = [response for response in responses if response.status_code == 201]
    codes = sorted(response.status_code for response in responses)
    assert len(created) == 1, f"бронь выдана {len(created)} раз вместо одного; коды: {codes}"

    # Остальные должны получить честный отказ, а не ошибку сервера.
    assert all(response.status_code == 409 for response in responses if response not in created), (
        f"неожиданные коды ответа: {codes}"
    )

    quantity, sold, held = await read_counters(session, sale_id)
    active = await count_active_reservations(session, sale_id)
    assert (quantity, sold, held, active) == (1, 0, 1, 1), (
        f"quantity={quantity}, sold={sold}, held={held}, активных броней={active}"
    )


async def test_held_matches_active_reservations(
    client: AsyncClient, session: AsyncSession, make_sale: MakeSale
) -> None:
    """ADR-002: счётчики в sales не расходятся с бронями."""
    sale_id = await make_sale(quantity=1)
    tokens = await login_buyers(client, RACE_BUYERS)

    await reserve_simultaneously(client, sale_id, tokens)

    await assert_counters_consistent(session, sale_id)
