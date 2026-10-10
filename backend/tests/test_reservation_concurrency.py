"""Остальные сценарии конкурентного доступа к броням.

Инвариант 1 и ADR-002. Участники запускаются синхронно (барьер) и каждый
на своём соединении — барьер снимается уже после захвата соединения,
поэтому параллельность не предполагается, а доказывается: тесты проверяют,
что `pg_backend_pid` у всех участников разные.

Число участников выбрано под пул SQLAlchemy (15 соединений), а не под
max_connections Postgres — см. CONCURRENT_PARTICIPANTS в conftest.
"""

from collections.abc import Awaitable, Callable
from uuid import UUID, uuid4

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.reservations import (
    CancelError,
    CancelOutcome,
    ReservationView,
    ReserveError,
    ReserveRefusal,
    cancel_reservation,
    create_reservation,
)
from tests.conftest import (
    CONCURRENT_PARTICIPANTS,
    MakeSale,
    assert_counters_consistent,
    count_active_reservations,
    read_counters,
    run_with_barrier,
)

# Раундов в тесте «отменяю и бронирую снова».
CANCEL_RESERVE_ROUNDS = 20

Operation = Callable[[AsyncSession, int], Awaitable[object]]


async def create_buyer(session: AsyncSession) -> UUID:
    """Покупатель, видимый другим соединениям, — поэтому с коммитом."""
    user_id = await session.scalar(
        text("insert into users (email, role) values (:email, 'buyer') returning id"),
        {"email": f"conc-{uuid4().hex[:12]}@example.com"},
    )
    await session.commit()
    return user_id


def successes(results: list[object]) -> list[ReservationView]:
    return [item for item in results if isinstance(item, ReservationView)]


def refusals(results: list[object]) -> list[ReserveRefusal]:
    return [item.reason for item in results if isinstance(item, ReserveError)]


def unexpected(results: list[object]) -> list[object]:
    """Всё, что не успех и не предусмотренный отказ, — повод разбираться."""
    return [
        item
        for item in results
        if not isinstance(item, ReservationView | ReserveError | CancelError | CancelOutcome)
        and item is not None
    ]


async def count_user_active(session: AsyncSession, sale_id: UUID, user_id: UUID) -> int:
    await session.rollback()
    count = await session.scalar(
        text("""
            select count(*) from reservations
            where sale_id = :sale_id and user_id = :user_id
              and status in ('held', 'paying', 'paid')
        """),
        {"sale_id": sale_id, "user_id": user_id},
    )
    return count or 0


async def test_same_buyer_reserving_ten_times_gets_one_unit(
    session: AsyncSession, make_sale: MakeSale
) -> None:
    """ADR-008: одна активная бронь на покупателя, даже если он жмёт десять раз.

    Товара заведомо хватает на всех — ограничивает именно частичный
    уникальный индекс, а не остаток.
    """
    sale_id = await make_sale(quantity=CONCURRENT_PARTICIPANTS)
    user_id = await create_buyer(session)

    async def reserve(participant_session: AsyncSession, _: int) -> object:
        return await create_reservation(participant_session, sale_id=sale_id, user_id=user_id)

    run = await run_with_barrier(CONCURRENT_PARTICIPANTS, reserve)

    assert run.distinct_connections == CONCURRENT_PARTICIPANTS, (
        f"участники делили соединения: различных {run.distinct_connections}"
    )
    assert not unexpected(run.results), f"неожиданные результаты: {unexpected(run.results)}"
    assert len(successes(run.results)) == 1, (
        f"выдано броней: {len(successes(run.results))}, отказы: {refusals(run.results)}"
    )
    assert refusals(run.results) == [ReserveRefusal.ALREADY_RESERVED] * (
        CONCURRENT_PARTICIPANTS - 1
    )

    _, sold, held = await read_counters(session, sale_id)
    assert (sold, held) == (0, 1), f"held={held} при одной выданной брони"
    await assert_counters_consistent(session, sale_id)


async def test_many_buyers_cannot_exceed_quantity(
    session: AsyncSession, make_sale: MakeSale
) -> None:
    """Покупателей вдвое больше, чем товара: разойдётся ровно quantity единиц."""
    quantity = 5
    sale_id = await make_sale(quantity=quantity)
    buyers = [await create_buyer(session) for _ in range(CONCURRENT_PARTICIPANTS)]

    async def reserve(participant_session: AsyncSession, index: int) -> object:
        return await create_reservation(participant_session, sale_id=sale_id, user_id=buyers[index])

    run = await run_with_barrier(CONCURRENT_PARTICIPANTS, reserve)

    assert run.distinct_connections == CONCURRENT_PARTICIPANTS
    assert not unexpected(run.results), f"неожиданные результаты: {unexpected(run.results)}"
    assert len(successes(run.results)) == quantity, (
        f"выдано {len(successes(run.results))} броней на {quantity} единиц"
    )
    assert refusals(run.results) == [ReserveRefusal.SOLD_OUT] * (CONCURRENT_PARTICIPANTS - quantity)

    _, sold, held = await read_counters(session, sale_id)
    assert (sold, held) == (0, quantity)
    assert await count_active_reservations(session, sale_id) == quantity
    await assert_counters_consistent(session, sale_id)


def cancel_or_reserve(current_id: UUID | None, sale_id: UUID, user_id: UUID) -> Operation:
    """Участник 0 отменяет текущую бронь, участник 1 бронирует новую."""

    async def operation(participant_session: AsyncSession, index: int) -> object:
        if index == 0:
            if current_id is None:
                return None
            return await cancel_reservation(
                participant_session, reservation_id=current_id, user_id=user_id
            )
        return await create_reservation(participant_session, sale_id=sale_id, user_id=user_id)

    return operation


async def test_cancel_and_reserve_in_parallel_keeps_one_reservation(
    session: AsyncSession, make_sale: MakeSale
) -> None:
    """Покупатель одновременно отменяет бронь и берёт новую, много раз подряд.

    Что бы ни выиграло гонку, активной брони у него не больше одной,
    а счётчики сходятся с реальностью после каждого раунда.
    """
    sale_id = await make_sale(quantity=3)
    user_id = await create_buyer(session)
    current_id: UUID | None = None

    for round_number in range(CANCEL_RESERVE_ROUNDS):
        run = await run_with_barrier(2, cancel_or_reserve(current_id, sale_id, user_id))

        assert run.distinct_connections == 2
        assert not unexpected(run.results), (
            f"раунд {round_number}: неожиданные результаты {unexpected(run.results)}"
        )

        active = await count_user_active(session, sale_id, user_id)
        assert active <= 1, f"раунд {round_number}: активных броней {active}"
        await assert_counters_consistent(session, sale_id)

        current_id = await session.scalar(
            text("""
                select id from reservations
                where sale_id = :sale_id and user_id = :user_id and status = 'held'
            """),
            {"sale_id": sale_id, "user_id": user_id},
        )

    await assert_counters_consistent(session, sale_id)


async def test_mixed_reserve_and_cancel_keeps_counters_consistent(
    session: AsyncSession, make_sale: MakeSale
) -> None:
    """Половина покупателей бронирует, половина отменяет свои брони — разом."""
    quantity = 7
    cancelling = CONCURRENT_PARTICIPANTS // 2
    sale_id = await make_sale(quantity=quantity)

    # Те, кто будет отменять, сначала получают брони по одной.
    cancellers = [await create_buyer(session) for _ in range(cancelling)]
    existing: list[UUID] = []
    for user_id in cancellers:
        reservation = await create_reservation(session, sale_id=sale_id, user_id=user_id)
        existing.append(reservation.id)

    _, _, held_before = await read_counters(session, sale_id)
    assert held_before == cancelling

    newcomers = [await create_buyer(session) for _ in range(CONCURRENT_PARTICIPANTS - cancelling)]

    async def mixed(participant_session: AsyncSession, index: int) -> object:
        if index < cancelling:
            return await cancel_reservation(
                participant_session,
                reservation_id=existing[index],
                user_id=cancellers[index],
            )
        return await create_reservation(
            participant_session,
            sale_id=sale_id,
            user_id=newcomers[index - cancelling],
        )

    run = await run_with_barrier(CONCURRENT_PARTICIPANTS, mixed)

    assert run.distinct_connections == CONCURRENT_PARTICIPANTS
    assert not unexpected(run.results), f"неожиданные результаты: {unexpected(run.results)}"

    quantity_db, sold, held = await read_counters(session, sale_id)
    active = await count_active_reservations(session, sale_id)
    assert held == active, f"held={held}, активных броней {active}"
    assert sold + held <= quantity_db, f"оверсейл: {sold} + {held} > {quantity_db}"
    await assert_counters_consistent(session, sale_id)

    # Все отмены прошли, а брони новичков ограничены остатком.
    assert all(isinstance(item, CancelOutcome) for item in run.results[:cancelling]), (
        f"отмены завершились иначе: {run.results[:cancelling]}"
    )


async def test_parallel_http_posts_for_the_last_unit(
    client: AsyncClient, session: AsyncSession, make_sale: MakeSale
) -> None:
    """Полный путь через HTTP: контракт API под конкурентной нагрузкой.

    Дополняет тест в test_reservation_race.py: тот сторожит инвариант,
    а этот — что наружу уходят ровно один 201 и честные 409 с машинным кодом.
    Барьер внутрь обработки запроса не вставить, поэтому здесь он синхронизирует
    только старт; соединение каждому запросу выдаёт зависимость сессии.
    """
    sale_id = await make_sale(quantity=1)
    tokens = []
    for _ in range(CONCURRENT_PARTICIPANTS):
        response = await client.post(
            "/api/auth/login", json={"email": f"http-{uuid4().hex[:12]}@example.com"}
        )
        tokens.append(response.json()["access_token"])

    async def post(_: AsyncSession, index: int) -> object:
        return await client.post(
            f"/api/sales/{sale_id}/reservations",
            headers={"Authorization": f"Bearer {tokens[index]}"},
        )

    run = await run_with_barrier(CONCURRENT_PARTICIPANTS, post)

    codes = sorted(response.status_code for response in run.results)
    assert codes == [201] + [409] * (CONCURRENT_PARTICIPANTS - 1), f"коды ответов: {codes}"

    conflict_codes = {
        response.json()["detail"]["code"] for response in run.results if response.status_code == 409
    }
    assert conflict_codes == {"sold_out"}, f"коды отказов: {conflict_codes}"

    # Витрина сразу показывает, что товара нет.
    sale = (await client.get(f"/api/sales/{sale_id}")).json()
    assert sale["remaining"] == 0

    await assert_counters_consistent(session, sale_id)
