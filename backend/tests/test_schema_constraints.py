"""Схема сама не даёт нарушить инварианты — проверяем на реальном Postgres."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import SaleFixture

INSERT_RESERVATION = text("""
    insert into reservations (sale_id, user_id, status, expires_at)
    values (:sale_id, :user_id, :status, now() + interval '10 minutes')
    returning id
""")


async def test_oversell_check_rejects_update(session: AsyncSession, sale: SaleFixture) -> None:
    """Инвариант 1: продано + удержано не может превысить quantity."""
    # quantity = 1, одна единица уже оплачена — распродажа исчерпана.
    await session.execute(
        text("update sales set sold = 1 where id = :sale_id"),
        {"sale_id": sale.sale_id},
    )

    # SAVEPOINT: после ошибки транзакция остаётся рабочей, данные фикстуры живы.
    with pytest.raises(IntegrityError) as exc_info:
        async with session.begin_nested():
            # Такой UPDATE в коде невозможен по условию `sold + held < quantity`,
            # но CHECK обязан держать оверсейл даже при баге.
            await session.execute(
                text("update sales set held = held + 1 where id = :sale_id"),
                {"sale_id": sale.sale_id},
            )

    assert "ck_sales_not_oversold" in str(exc_info.value)

    # Счётчики не изменились.
    sold, held = (
        await session.execute(
            text("select sold, held from sales where id = :sale_id"),
            {"sale_id": sale.sale_id},
        )
    ).one()
    assert (sold, held) == (1, 0)


async def test_one_active_reservation_per_user(session: AsyncSession, sale: SaleFixture) -> None:
    """ADR-008: одна активная бронь на покупателя в распродаже.

    Завершённая бронь освобождает место — покупатель может взять новую.
    """
    first_reservation_id = await session.scalar(
        INSERT_RESERVATION,
        {"sale_id": sale.sale_id, "user_id": sale.buyer_id, "status": "held"},
    )
    assert first_reservation_id is not None

    with pytest.raises(IntegrityError) as exc_info:
        async with session.begin_nested():
            await session.execute(
                INSERT_RESERVATION,
                {"sale_id": sale.sale_id, "user_id": sale.buyer_id, "status": "held"},
            )

    assert "uq_reservations_sale_id_user_id_active" in str(exc_info.value)

    # Первая бронь истекла — частичный индекс её больше не видит.
    await session.execute(
        text("update reservations set status = 'expired' where id = :reservation_id"),
        {"reservation_id": first_reservation_id},
    )

    second_reservation_id = await session.scalar(
        INSERT_RESERVATION,
        {"sale_id": sale.sale_id, "user_id": sale.buyer_id, "status": "held"},
    )
    assert second_reservation_id is not None
    assert second_reservation_id != first_reservation_id
