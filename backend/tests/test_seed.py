"""Seed-скрипт: повторный запуск не ломается и не плодит сущности."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.scripts.seed import BUYER_EMAILS, PRODUCT_NAME, SHOP_EMAIL, parse_args, seed

SEED_EMAILS = (SHOP_EMAIL, *BUYER_EMAILS)


async def count_rows(session: AsyncSession, sql: str, params: dict) -> int:
    await session.rollback()  # seed коммитит в своей транзакции
    return await session.scalar(text(sql), params)


async def test_seed_is_idempotent(session: AsyncSession) -> None:
    first = await seed(session, quantity=5, start_in=2, duration=30)
    second = await seed(session, quantity=5, start_in=2, duration=30)

    # Пользователи и товар переиспользованы, не созданы заново.
    assert second.shop_id == first.shop_id
    assert second.buyer_ids == first.buyer_ids
    assert second.product_id == first.product_id

    assert await count_rows(
        session,
        "select count(*) from users where email = any(:emails)",
        {"emails": list(SEED_EMAILS)},
    ) == len(SEED_EMAILS)
    assert (
        await count_rows(
            session,
            "select count(*) from products where name = :name",
            {"name": PRODUCT_NAME},
        )
        == 1
    )
    # Распродажа без броней переиспользуется, а не дублируется.
    assert second.sale_id == first.sale_id
    assert second.sale_reused is True
    assert (
        await count_rows(
            session,
            "select count(*) from sales where product_id = :product_id",
            {"product_id": first.product_id},
        )
        == 1
    )


async def test_seed_creates_new_sale_when_previous_has_reservations(
    session: AsyncSession,
) -> None:
    """Распродажу с бронями трогать нельзя: sold/held разъехались бы
    со строками в reservations (ADR-002)."""
    first = await seed(session)
    await session.execute(
        text("""
            insert into reservations (sale_id, user_id, status, expires_at)
            values (:sale_id, :user_id, 'held', now() + interval '10 minutes')
        """),
        {"sale_id": first.sale_id, "user_id": first.buyer_ids[0]},
    )
    await session.commit()

    second = await seed(session)

    assert second.sale_id != first.sale_id
    assert second.sale_reused is False


async def test_seed_window_comes_from_database_time(session: AsyncSession) -> None:
    result = await seed(session, quantity=7, start_in=2, duration=30)

    assert result.quantity == 7
    assert (result.end_at - result.start_at).total_seconds() == 30 * 60

    await session.rollback()
    db_now = await session.scalar(text("select now()"))
    minutes_to_start = (result.start_at - db_now).total_seconds() / 60
    assert 1 < minutes_to_start <= 2


def test_parse_args_defaults() -> None:
    args = parse_args([])
    assert (args.quantity, args.start_in, args.duration) == (5, 2, 30)


def test_parse_args_rejects_empty_sale() -> None:
    for argv in (["--quantity", "0"], ["--duration", "0"]):
        try:
            parse_args(argv)
        except SystemExit as exit_error:
            assert exit_error.code == 2
        else:
            raise AssertionError(f"ожидалась ошибка разбора аргументов для {argv}")
