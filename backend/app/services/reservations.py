"""Резерв единицы товара.

Инвариант 1 и ADR-002: продано + удержано никогда не больше quantity.
Единственный способ занять единицу — атомарный условный UPDATE. Никакого
«прочитали остаток → проверили в Python → записали»: между чтением и записью
влезает конкурент, и товар уходит дважды.

Инвариант 2: окно распродажи проверяет сама БД в том же UPDATE, по now().
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import ReservationStatus

logger = logging.getLogger(__name__)

# Имя частичного уникального индекса из первой миграции. По нему отличаем
# «у покупателя уже есть активная бронь» от прочих нарушений целостности.
ACTIVE_RESERVATION_INDEX = "uq_reservations_sale_id_user_id_active"


class ReserveRefusal(StrEnum):
    """Почему бронь не выдана. Код уходит клиенту — фронт ветвится по нему."""

    SALE_NOT_FOUND = "sale_not_found"
    NOT_STARTED = "not_started"
    ENDED = "ended"
    SOLD_OUT = "sold_out"
    ALREADY_RESERVED = "already_reserved"


class ReserveError(Exception):
    def __init__(self, reason: ReserveRefusal) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True)
class ReservationView:
    id: UUID
    sale_id: UUID
    status: str
    expires_at: datetime
    created_at: datetime


# Единственное место, где занимается единица товара.
#
# held = held + 1 — инкремент, а не посчитанное в Python значение: Postgres
# блокирует строку и применяет приращение к актуальным данным. Условия в WHERE
# проверяются тем же оператором, поэтому между проверкой и записью нет зазора.
# Нет строки в ответе — значит занять не удалось; причину выясняем отдельно
# и только для текста ответа (docs/DATA_MODEL.md).
HOLD_UNIT_SQL = text("""
    update sales set held = held + 1
    where id = :sale_id
      and now() >= start_at
      and now() < end_at
      and closed_at is null
      and sold + held < quantity
    returning id
""")

# Запускается, только если UPDATE не выдал строку. Решение уже принято —
# здесь лишь выбираем, что сказать клиенту.
EXPLAIN_REFUSAL_SQL = text("""
    select
        now() < start_at                      as not_started,
        closed_at is not null or now() >= end_at as ended,
        sold + held >= quantity               as sold_out
    from sales
    where id = :sale_id
""")

INSERT_RESERVATION_SQL = text("""
    insert into reservations (sale_id, user_id, status, expires_at)
    values (:sale_id, :user_id, :status, now() + make_interval(secs => :ttl_seconds))
    returning id, sale_id, status, expires_at, created_at
""")


async def explain_refusal(session: AsyncSession, sale_id: UUID) -> ReserveRefusal:
    row = (await session.execute(EXPLAIN_REFUSAL_SQL, {"sale_id": sale_id})).one_or_none()
    if row is None:
        return ReserveRefusal.SALE_NOT_FOUND
    if row.not_started:
        return ReserveRefusal.NOT_STARTED
    if row.ended:
        return ReserveRefusal.ENDED
    if row.sold_out:
        return ReserveRefusal.SOLD_OUT
    # Распродажа идёт и место есть, но UPDATE строку не выдал: состояние
    # изменилось между двумя запросами. Для клиента это «разобрали».
    logger.info("резерв не удался, состояние изменилось между запросами (sale_id=%s)", sale_id)
    return ReserveRefusal.SOLD_OUT


async def create_reservation(
    session: AsyncSession,
    *,
    sale_id: UUID,
    user_id: UUID,
) -> ReservationView:
    """Занять одну единицу и создать бронь в held. Всё в одной транзакции.

    При отказе транзакция откатывается целиком, поэтому held никогда
    не остаётся увеличенным без соответствующей брони.
    """
    try:
        held = await session.scalar(HOLD_UNIT_SQL, {"sale_id": sale_id})
        if held is None:
            raise ReserveError(await explain_refusal(session, sale_id))

        row = (
            await session.execute(
                INSERT_RESERVATION_SQL,
                {
                    "sale_id": sale_id,
                    "user_id": user_id,
                    "status": ReservationStatus.HELD.value,
                    "ttl_seconds": get_settings().reservation_ttl_seconds,
                },
            )
        ).one()
        await session.commit()
    except IntegrityError as error:
        # «Уже есть активная бронь» решает частичный уникальный индекс, а не
        # предварительный SELECT: проверка перед вставкой была бы той же
        # гонкой. Откат снимает и инкремент held.
        await session.rollback()
        if ACTIVE_RESERVATION_INDEX in str(error):
            raise ReserveError(ReserveRefusal.ALREADY_RESERVED) from error
        raise
    except ReserveError:
        await session.rollback()
        raise

    return ReservationView(
        id=row.id,
        sale_id=row.sale_id,
        status=row.status,
        expires_at=row.expires_at,
        created_at=row.created_at,
    )
