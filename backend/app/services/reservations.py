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
from app.services.server_time import get_server_time

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


class CancelRefusal(StrEnum):
    """Почему бронь не отменена."""

    RESERVATION_NOT_FOUND = "reservation_not_found"
    NOT_CANCELLABLE = "not_cancellable"


class CancelError(Exception):
    def __init__(self, reason: CancelRefusal) -> None:
        super().__init__(reason.value)
        self.reason = reason


class CancelOutcome(StrEnum):
    """Чем закончилась отмена. Клиенту в обоих случаях уходит 204."""

    CANCELLED = "cancelled"
    # Бронь уже была завершена, единица давно вернулась — делать нечего.
    ALREADY_RELEASED = "already_released"


# Терминальные статусы, при которых единица товара уже возвращена: повторная
# отмена ничего не меняет и отвечает так же, как успешная.
RELEASED_STATUSES = frozenset(
    {
        ReservationStatus.CANCELLED,
        ReservationStatus.EXPIRED,
        ReservationStatus.FAILED,
    }
)

# Инвариант 4: начатая оплата доводится до конца, отменять её нельзя.
NOT_CANCELLABLE_STATUSES = frozenset(
    {
        ReservationStatus.PAYING,
        ReservationStatus.PAID,
    }
)

# Брони, которые покупатель видит как «свои активные».
BUYER_ACTIVE_STATUSES = (ReservationStatus.HELD, ReservationStatus.PAYING)
_BUYER_ACTIVE_SQL = ", ".join(f"'{status.value}'" for status in BUYER_ACTIVE_STATUSES)


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


# Отмена. Решение принимает БД: условие `status = 'held'` проверяется тем же
# оператором, что и запись, поэтому две одновременные отмены не уменьшат held
# дважды. Проигравшие получат 0 строк и не тронут счётчик.
CANCEL_HELD_SQL = text(f"""
    update reservations
    set status = '{ReservationStatus.CANCELLED.value}', updated_at = now()
    where id = :reservation_id
      and user_id = :user_id
      and status = '{ReservationStatus.HELD.value}'
    returning sale_id
""")

# Триггера на updated_at в БД нет, и ORM-onupdate на явном SQL не срабатывает,
# поэтому время правки пишется выше руками.
RELEASE_UNIT_SQL = text("update sales set held = held - 1 where id = :sale_id")

# sale_id брони неизменяем, поэтому читается обычным SELECT без блокировки:
# нужен он только чтобы узнать, какую строку sales захватывать первой.
# Фильтр по user_id избавляет от захвата чужой распродажи — решение о том,
# можно ли отменять, всё равно принимает условный UPDATE ниже.
RESERVATION_SALE_SQL = text("""
    select sale_id from reservations
    where id = :reservation_id and user_id = :user_id
""")

# Инвариант 10: порядок блокировок sales → reservations. Резерв берёт строку
# sales первой (held = held + 1), поэтому и отмена обязана начинать с неё.
# Пока отмена захватывала бронь раньше распродажи, одновременные отмена
# и резерв одного покупателя давали взаимную блокировку: резерв держал sales
# и ждал уникальный индекс по reservations, отмена держала бронь и ждала
# sales (ADR-018).
LOCK_SALE_SQL = text("select id from sales where id = :sale_id for update")

# Запускается, только если UPDATE не изменил строку, — исключительно чтобы
# выбрать ответ.
RESERVATION_STATE_SQL = text("select user_id, status from reservations where id = :reservation_id")

LIST_BUYER_ACTIVE_SQL = text(f"""
    select r.id, r.sale_id, r.status, r.expires_at
    from reservations r
    where r.user_id = :user_id
      and r.status in ({_BUYER_ACTIVE_SQL})
    order by r.created_at desc
""")


@dataclass(frozen=True)
class MyReservationItem:
    id: UUID
    sale_id: UUID
    status: str
    expires_at: datetime


@dataclass(frozen=True)
class MyReservationsView:
    # Одно время на весь ответ: клиент считает остаток брони по смещению
    # от него, а не по часам браузера (ADR-009).
    server_time: datetime
    reservations: list[MyReservationItem]


async def list_my_active_reservations(
    session: AsyncSession, *, user_id: UUID
) -> MyReservationsView:
    """Брони покупателя в held и paying, свежие сверху."""
    server_time = await get_server_time(session)
    result = await session.execute(LIST_BUYER_ACTIVE_SQL, {"user_id": user_id})
    return MyReservationsView(
        server_time=server_time,
        reservations=[MyReservationItem(**row._mapping) for row in result],
    )


async def explain_cancel_failure(
    session: AsyncSession, reservation_id: UUID, user_id: UUID
) -> CancelOutcome:
    """Почему UPDATE не изменил строку. Решение уже принято, это только ответ."""
    row = (
        await session.execute(RESERVATION_STATE_SQL, {"reservation_id": reservation_id})
    ).one_or_none()

    # Чужую бронь не отличаем от несуществующей: иначе по коду ответа можно
    # было бы перебором выяснять, какие брони существуют.
    if row is None or row.user_id != user_id:
        raise CancelError(CancelRefusal.RESERVATION_NOT_FOUND)

    status = ReservationStatus(row.status)
    if status in RELEASED_STATUSES:
        return CancelOutcome.ALREADY_RELEASED
    if status in NOT_CANCELLABLE_STATUSES:
        raise CancelError(CancelRefusal.NOT_CANCELLABLE)

    # Статус held, но UPDATE строку не нашёл: состояние изменилось между двумя
    # запросами. Защитная ветка, в норме недостижима.
    logger.warning(
        "отмена не удалась при статусе held, состояние изменилось (reservation_id=%s)",
        reservation_id,
    )
    raise CancelError(CancelRefusal.NOT_CANCELLABLE)


async def cancel_reservation(
    session: AsyncSession,
    *,
    reservation_id: UUID,
    user_id: UUID,
) -> CancelOutcome:
    """Отменить свою бронь в held и вернуть единицу товара.

    Смена статуса и уменьшение held — в одной транзакции (ADR-002).

    Порядок блокировок — sales, затем reservations (инвариант 10, ADR-018).
    Решение по-прежнему принимает условный UPDATE по `status = 'held'`:
    захват строки sales лишь выстраивает порядок и ничего не решает.
    """
    try:
        sale_id = await session.scalar(
            RESERVATION_SALE_SQL, {"reservation_id": reservation_id, "user_id": user_id}
        )
        if sale_id is None:
            # Брони нет или она чужая — explain вернёт 404.
            outcome = await explain_cancel_failure(session, reservation_id, user_id)
        else:
            await session.execute(LOCK_SALE_SQL, {"sale_id": sale_id})
            cancelled_sale_id = await session.scalar(
                CANCEL_HELD_SQL, {"reservation_id": reservation_id, "user_id": user_id}
            )
            if cancelled_sale_id is not None:
                await session.execute(RELEASE_UNIT_SQL, {"sale_id": cancelled_sale_id})
                await session.commit()
                return CancelOutcome.CANCELLED

            outcome = await explain_cancel_failure(session, reservation_id, user_id)
    except CancelError:
        await session.rollback()
        raise

    await session.rollback()  # ничего не меняли
    return outcome


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
