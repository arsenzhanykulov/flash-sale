"""Закрытие распродаж по окончании окна (ADR-025).

Когда `end_at` прошёл, распродажа закрывается: неоплаченные брони в `held`
снимаются, корзины очищаются, владельцам уходит письмо. `quantity` не трогаем —
магазину нужна цифра «сколько не продано» (`quantity - sold`).

Инвариант 4: брони в `paying` не снимаются. Начатая оплата доводится до конца,
и успешный вебхук после закрытия по-прежнему делает заказ `paid`.

Инвариант 6: письмо пишется в `outbox` в той же транзакции, что и снятие брони,
с уникальным `dedup_key` (ADR-004).

Инвариант 10: порядок захвата — `sales`, затем `reservations`.
"""

import asyncio
import logging
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.models import ReservationStatus

logger = logging.getLogger(__name__)


def cart_released_dedup_key(reservation_id: UUID) -> str:
    """Письмо на каждую очищенную корзину, а не на распродажу."""
    return f"sale_closed_cart_released:{reservation_id}"


# Обычный SELECT без блокировки: только чтобы узнать объём работы.
SALES_TO_CLOSE_SQL = text("""
    select id from sales
    where end_at <= now() and closed_at is null
    order by end_at
""")

# Инвариант 10: sales первой. Условие повторяется в WHERE намеренно — второй
# воркер, дождавшись блокировки, не найдёт строку (closed_at уже заполнен)
# и ничего не сделает. Это и есть защита от двойного закрытия.
LOCK_SALE_TO_CLOSE_SQL = text("""
    select id from sales
    where id = :sale_id and end_at <= now() and closed_at is null
    for update
""")

# status = 'held' оберегает paying: инвариант 4.
RELEASE_HOLDS_SQL = text(f"""
    update reservations
    set status = '{ReservationStatus.CANCELLED.value}', updated_at = now()
    where sale_id = :sale_id and status = '{ReservationStatus.HELD.value}'
    returning id
""")

RELEASE_UNITS_SQL = text("update sales set held = held - :released where id = :sale_id")

# Два UPDATE sales в одной транзакции (held и closed_at) дадут один NOTIFY:
# Postgres склеивает одинаковые (канал, payload) внутри транзакции (ADR-020).
MARK_CLOSED_SQL = text("update sales set closed_at = now() where id = :sale_id")

# Письмо каждому владельцу очищенной корзины. Данные берутся одним запросом
# от самой брони: получатель, товар и распродажа.
#
# cast(... as text), а не `::text`: двоеточие рядом с именованным параметром
# SQLAlchemy разбирает как часть его имени.
INSERT_CART_RELEASED_SQL = text("""
    insert into outbox (kind, dedup_key, recipient, payload, status)
    select
        'sale_closed_cart_released',
        :dedup_key,
        u.email,
        jsonb_build_object(
            'sale_id', cast(s.id as text),
            'reservation_id', cast(r.id as text),
            'product_name', p.name
        ),
        'pending'
    from reservations r
    join sales s on s.id = r.sale_id
    join products p on p.id = s.product_id
    join users u on u.id = r.user_id
    where r.id = :reservation_id
    on conflict (dedup_key) do nothing
""")


@dataclass(frozen=True)
class CloseReport:
    closed: int = 0
    released: int = 0
    letters: int = 0


async def close_one_sale(session: AsyncSession, sale_id: UUID) -> tuple[int, int]:
    """Закрыть одну распродажу. Возвращает (снято броней, писем).

    Транзакция на распродажу, а не на весь проход: иначе одна долгая строка
    держала бы блокировки на всех остальных (как в свипере, ADR-019).
    """
    locked = await session.scalar(LOCK_SALE_TO_CLOSE_SQL, {"sale_id": sale_id})
    if locked is None:
        # Другой воркер успел закрыть, или окно ещё не истекло.
        await session.rollback()
        return 0, 0

    released = (await session.execute(RELEASE_HOLDS_SQL, {"sale_id": sale_id})).all()
    if released:
        await session.execute(RELEASE_UNITS_SQL, {"sale_id": sale_id, "released": len(released)})

    letters = 0
    for reservation in released:
        result = await session.execute(
            INSERT_CART_RELEASED_SQL,
            {
                "dedup_key": cart_released_dedup_key(reservation.id),
                "reservation_id": reservation.id,
            },
        )
        letters += result.rowcount or 0

    await session.execute(MARK_CLOSED_SQL, {"sale_id": sale_id})
    await session.commit()

    logger.info(
        "распродажа %s закрыта: снято броней %d, писем %d",
        sale_id,
        len(released),
        letters,
    )
    return len(released), letters


async def close_sales_once(session: AsyncSession) -> CloseReport:
    """Один проход. Отдельно от цикла — чтобы звать из тестов напрямую."""
    sale_ids: list[UUID] = list((await session.scalars(SALES_TO_CLOSE_SQL)).all())
    await session.rollback()
    if not sale_ids:
        return CloseReport()

    closed = released_total = letters_total = 0
    for sale_id in sale_ids:
        released, letters = await close_one_sale(session, sale_id)
        # Считаем закрытой только ту, которую закрыли мы: вторую попытку
        # по той же распродаже LOCK_SALE_TO_CLOSE_SQL отсечёт.
        if await was_closed_by_us(session, sale_id):
            closed += 1
        released_total += released
        letters_total += letters

    return CloseReport(closed=closed, released=released_total, letters=letters_total)


async def was_closed_by_us(session: AsyncSession, sale_id: UUID) -> bool:
    """Распродажа закрыта — безразлично, нами или встречным воркером."""
    await session.rollback()
    closed_at = await session.scalar(
        text("select closed_at from sales where id = :sale_id"), {"sale_id": sale_id}
    )
    return closed_at is not None


async def run_sale_closer() -> None:
    """Фоновый цикл: первый проход сразу (ADR-010).

    Распродажи, закончившиеся за время простоя сервера, закрываются при старте:
    работа выбирается запросом к БД, а не таймерами в памяти.
    """
    interval = get_settings().sale_closer_interval_seconds
    logger.info("закрытие распродаж запущено, интервал %d с", interval)

    while True:
        try:
            async with get_sessionmaker()() as session:
                await close_sales_once(session)
        except asyncio.CancelledError:
            logger.info("закрытие распродаж остановлено")
            raise
        except Exception:
            logger.exception("итерация закрытия распродаж упала, цикл продолжается")

        await asyncio.sleep(interval)
