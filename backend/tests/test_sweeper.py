"""Свипер истёкших броней.

Итерация `sweep_expired_once` вызывается напрямую — фоновый цикл в тестах
не запускается: `ASGITransport` не прогоняет lifespan.

`expires_at` в прошлое задаётся здесь же, ждать настоящий TTL не нужно.
"""

from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.reservations import cancel_reservation, create_reservation
from app.services.sweeper import expire_holds_of_sale, sweep_expired_once
from tests.conftest import (
    CONCURRENT_PARTICIPANTS,
    MakeSale,
    assert_counters_consistent,
    count_active_reservations,
    read_counters,
    run_with_barrier,
)

DEADLOCK_ROUNDS = 20


async def create_buyer(session: AsyncSession) -> UUID:
    user_id = await session.scalar(
        text("insert into users (email, role) values (:email, 'buyer') returning id"),
        {"email": f"sweep-{uuid4().hex[:12]}@example.com"},
    )
    await session.commit()
    return user_id


async def add_reservation(
    session: AsyncSession,
    sale_id: UUID,
    *,
    status: str = "held",
    expires_at: str = "now() - interval '1 minute'",
) -> UUID:
    """Бронь с заданным статусом и сроком, и соответствующий ей held + 1.

    Выражение срока подставляется в SQL из кода теста — так срок считается
    по времени БД, а не по часам Python.
    """
    user_id = await create_buyer(session)
    reservation_id = await session.scalar(
        text(f"""
            insert into reservations (sale_id, user_id, status, expires_at)
            values (:sale_id, :user_id, :status, {expires_at})
            returning id
        """),
        {"sale_id": sale_id, "user_id": user_id, "status": status},
    )
    # held считает и held, и paying (docs/DATA_MODEL.md).
    await session.execute(
        text("update sales set held = held + 1 where id = :sale_id"), {"sale_id": sale_id}
    )
    await session.commit()
    return reservation_id


async def reservation_status(session: AsyncSession, reservation_id: UUID) -> str | None:
    await session.rollback()
    return await session.scalar(
        text("select status from reservations where id = :reservation_id"),
        {"reservation_id": reservation_id},
    )


def exceptions(results: list[object]) -> list[object]:
    return [item for item in results if isinstance(item, BaseException)]


def database_errors(results: list[object]) -> list[object]:
    """Ошибки уровня БД — дедлоки, обрывы, нарушения ограничений.

    Отказы предметной области (`sold_out`, `already_reserved`) сюда не
    попадают: это штатный результат участника, а не сбой.
    """
    return [item for item in results if isinstance(item, SQLAlchemyError)]


async def test_expired_hold_is_released(session: AsyncSession, make_sale: MakeSale) -> None:
    sale_id = await make_sale(quantity=3)
    reservation_id = await add_reservation(session, sale_id)
    _, _, held_before = await read_counters(session, sale_id)
    assert held_before == 1

    # По своей распродаже: sweep_expired_once идёт по всей базе, где лежат
    # брони других тестов, и глобальное число ничего не доказывало бы.
    released = await expire_holds_of_sale(session, sale_id)

    assert released == 1
    assert await reservation_status(session, reservation_id) == "expired"
    quantity, sold, held = await read_counters(session, sale_id)
    assert (sold, held) == (0, 0)
    # Единица вернулась на витрину.
    assert quantity - sold - held == 3
    await assert_counters_consistent(session, sale_id)


async def test_fresh_hold_is_untouched(session: AsyncSession, make_sale: MakeSale) -> None:
    sale_id = await make_sale(quantity=3)
    reservation_id = await add_reservation(
        session, sale_id, expires_at="now() + interval '10 minutes'"
    )

    released = await expire_holds_of_sale(session, sale_id)

    assert released == 0
    assert await reservation_status(session, reservation_id) == "held"
    _, sold, held = await read_counters(session, sale_id)
    assert (sold, held) == (0, 1)
    await assert_counters_consistent(session, sale_id)


async def test_paying_is_untouched_even_when_expired(
    session: AsyncSession, make_sale: MakeSale
) -> None:
    """Инвариант 4: начатая оплата доводится до конца, срок её не касается."""
    sale_id = await make_sale(quantity=3)
    reservation_id = await add_reservation(
        session, sale_id, status="paying", expires_at="now() - interval '1 hour'"
    )

    released = await expire_holds_of_sale(session, sale_id)

    assert released == 0
    assert await reservation_status(session, reservation_id) == "paying"
    _, sold, held = await read_counters(session, sale_id)
    assert (sold, held) == (0, 1), "свипер тронул бронь в paying"
    await assert_counters_consistent(session, sale_id)


async def test_several_sales_in_one_pass(session: AsyncSession, make_sale: MakeSale) -> None:
    first = await make_sale(quantity=3)
    second = await make_sale(quantity=2)
    third = await make_sale(quantity=5)

    await add_reservation(session, first)
    await add_reservation(session, first)
    await add_reservation(session, second)
    # Третья распродажа: бронь ещё живёт, трогать нельзя.
    fresh = await add_reservation(session, third, expires_at="now() + interval '5 minutes'")

    # Один проход обрабатывает все распродажи сразу. Точное число не
    # проверяем: в базе лежат брони других тестов, поэтому сверяем >= своих
    # трёх, а главное — счётчики каждой распродажи ниже.
    released = await sweep_expired_once(session)

    assert released >= 3, f"вернулось {released} единиц, ожидалось минимум 3"
    for sale_id, expected_held in ((first, 0), (second, 0), (third, 1)):
        _, sold, held = await read_counters(session, sale_id)
        assert (sold, held) == (0, expected_held), f"распродажа {sale_id}: held={held}"
        await assert_counters_consistent(session, sale_id)
    assert await reservation_status(session, fresh) == "held"


async def test_ten_sweepers_release_each_unit_once(
    session: AsyncSession, make_sale: MakeSale
) -> None:
    """Несколько экземпляров сервиса не вернут одну единицу дважды.

    Решает условие `status = 'held'` в UPDATE: проигравший свипер, дождавшись
    блокировки, видит уже `expired` и получает 0 строк в RETURNING (ADR-019).
    """
    first = await make_sale(quantity=5)
    second = await make_sale(quantity=5)
    expired_count = 4
    for _ in range(2):
        await add_reservation(session, first)
        await add_reservation(session, second)

    async def sweep(participant_session: AsyncSession, _: int) -> object:
        return await sweep_expired_once(participant_session)

    run = await run_with_barrier(CONCURRENT_PARTICIPANTS, sweep)

    assert run.distinct_connections == CONCURRENT_PARTICIPANTS
    assert not exceptions(run.results), f"свиперы упали: {exceptions(run.results)}"
    assert sum(run.results) == expired_count, (
        f"единицы вернули {sum(run.results)} раз вместо {expired_count}: {run.results}"
    )

    for sale_id in (first, second):
        _, sold, held = await read_counters(session, sale_id)
        assert (sold, held) == (0, 0), f"распродажа {sale_id}: held={held}"
        assert await count_active_reservations(session, sale_id) == 0
        await assert_counters_consistent(session, sale_id)


async def test_sweeper_and_cancel_release_the_unit_once(
    session: AsyncSession, make_sale: MakeSale
) -> None:
    """Свипер и отмена той же брони одновременно: held уменьшается один раз."""
    sale_id = await make_sale(quantity=3)
    user_id = await create_buyer(session)
    reservation = await create_reservation(session, sale_id=sale_id, user_id=user_id)
    await session.execute(
        text("""
            update reservations set expires_at = now() - interval '1 minute'
            where id = :reservation_id
        """),
        {"reservation_id": reservation.id},
    )
    await session.commit()
    _, _, held_before = await read_counters(session, sale_id)
    assert held_before == 1

    async def sweep_or_cancel(participant_session: AsyncSession, index: int) -> object:
        if index == 0:
            return await sweep_expired_once(participant_session)
        return await cancel_reservation(
            participant_session, reservation_id=reservation.id, user_id=user_id
        )

    run = await run_with_barrier(2, sweep_or_cancel)

    assert not exceptions(run.results), f"участники упали: {exceptions(run.results)}"
    _, sold, held = await read_counters(session, sale_id)
    assert (sold, held) == (0, 0), f"held уменьшился не один раз: {held}"
    assert await reservation_status(session, reservation.id) in {"expired", "cancelled"}
    await assert_counters_consistent(session, sale_id)


async def test_sweeper_and_reserve_do_not_deadlock(
    session: AsyncSession, make_sale: MakeSale
) -> None:
    """Инвариант 10: свипер и резерв берут sales первой, порядок не инвертируется.

    Повторяем много раз: дедлок — гонка, одного раунда мало.

    Товара с запасом на все раунды: за раунд held растёт на единицу (бронь
    резерва остаётся живой), и на исчерпанной распродаже условный UPDATE
    отказывал бы сразу, перестав конкурировать со свипером — тест бы
    проверял уже не то.
    """
    sale_id = await make_sale(quantity=DEADLOCK_ROUNDS * 2)

    for round_number in range(DEADLOCK_ROUNDS):
        await add_reservation(session, sale_id)
        buyer_id = await create_buyer(session)

        async def sweep_or_reserve(
            participant_session: AsyncSession, index: int, user_id: UUID = buyer_id
        ) -> object:
            if index == 0:
                return await sweep_expired_once(participant_session)
            return await create_reservation(participant_session, sale_id=sale_id, user_id=user_id)

        run = await run_with_barrier(2, sweep_or_reserve)

        # Проверяем именно отсутствие дедлоков: отказ по остатку был бы
        # штатным результатом, а не сбоем.
        assert not database_errors(run.results), (
            f"раунд {round_number}: {database_errors(run.results)}"
        )
        await assert_counters_consistent(session, sale_id)


async def test_sweep_does_nothing_when_nothing_expired(
    session: AsyncSession, make_sale: MakeSale
) -> None:
    await make_sale(quantity=3)

    assert await sweep_expired_once(session) == 0
