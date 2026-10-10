"""Свипер истёкших броней.

Инвариант 3: бронь в `held` живёт `RESERVATION_TTL_SECONDS`, после чего
единица товара обязана вернуться на витрину. Инвариант 4: бронь в `paying`
не истекает — начатая оплата доводится до конца, поэтому свипер её не видит.

ADR-010: работа выбирается запросом к БД, а не из таймеров в памяти, поэтому
перезапуск сервера ничего не теряет — просроченное обработается при старте.
"""

import asyncio
import logging
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.models import ReservationStatus
from app.services.locks import lock_sale

logger = logging.getLogger(__name__)

# Обычный SELECT без блокировки: только чтобы узнать, какие распродажи
# вообще нужно обрабатывать. Идёт по индексу ix_reservations_status_expires_at.
SALES_WITH_EXPIRED_SQL = text(f"""
    select distinct sale_id from reservations
    where status = '{ReservationStatus.HELD.value}' and expires_at <= now()
""")

# Решение принимает это условие, а не предварительное чтение: RETURNING даёт
# ровно те брони, которые перевела в expired именно эта транзакция. Второй
# свипер, дождавшись блокировки строк, увидит уже expired и получит 0 строк —
# поэтому единица не возвращается дважды (ADR-019).
#
# status = 'held' заодно оберегает paying: инвариант 4.
# Время — только из БД (инвариант 2).
EXPIRE_HELD_SQL = text(f"""
    update reservations
    set status = '{ReservationStatus.EXPIRED.value}', updated_at = now()
    where sale_id = :sale_id
      and status = '{ReservationStatus.HELD.value}'
      and expires_at <= now()
    returning id
""")

RELEASE_UNITS_SQL = text("update sales set held = held - :released where id = :sale_id")


async def sweep_expired_once(session: AsyncSession) -> int:
    """Один проход. Возвращает, сколько единиц вернулось на витрину.

    Отдельная функция от цикла — чтобы её можно было звать из тестов напрямую.
    """
    sale_ids: list[UUID] = list((await session.scalars(SALES_WITH_EXPIRED_SQL)).all())
    # Закрываем транзакцию поиска: дальше на каждую распродажу своя.
    await session.rollback()

    released_total = 0
    for sale_id in sale_ids:
        released_total += await expire_holds_of_sale(session, sale_id)
    return released_total


async def expire_holds_of_sale(session: AsyncSession, sale_id: UUID) -> int:
    """Одна распродажа, одна транзакция.

    Транзакция на распродажу, а не на весь проход: иначе одна долгая строка
    держала бы блокировки на всех остальных распродажах.
    """
    # Инвариант 10: sales раньше reservations. Без этого свипер брал бы строки
    # в обратном порядке относительно резерва — ровно та инверсия, которая
    # дала дедлок на шаге 2.3 (ADR-018).
    await lock_sale(session, sale_id)

    expired = list((await session.scalars(EXPIRE_HELD_SQL, {"sale_id": sale_id})).all())
    if expired:
        await session.execute(RELEASE_UNITS_SQL, {"sale_id": sale_id, "released": len(expired)})
    await session.commit()

    if expired:
        logger.info("свипер вернул %d единиц в распродаже %s", len(expired), sale_id)
    return len(expired)


async def run_sweeper() -> None:
    """Фоновый цикл: первый проход сразу, дальше по интервалу (ADR-010).

    Ошибка одной итерации логируется и не убивает цикл; остановку
    (`CancelledError`) пропускаем наружу, чтобы сервер мог завершиться.
    """
    interval = get_settings().sweeper_interval_seconds
    logger.info("свипер запущен, интервал %d с", interval)

    while True:
        try:
            async with get_sessionmaker()() as session:
                await sweep_expired_once(session)
        except asyncio.CancelledError:
            logger.info("свипер остановлен")
            raise
        except Exception:
            logger.exception("итерация свипера упала, цикл продолжается")

        await asyncio.sleep(interval)
