"""Резерв единицы товара.

Шаг 2.0: здесь лежит НАИВНАЯ реализация, см. предупреждение ниже.
"""

import asyncio
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

# Инвариант 3: бронь в held живёт 10 минут. Срок задаёт сервис, а не схема:
# это бизнес-правило, в БД его смена требовала бы миграции.
RESERVATION_TTL_MINUTES = 10

# Имя частичного уникального индекса из первой миграции. По нему отличаем
# «у покупателя уже есть активная бронь» от прочих нарушений целостности.
ACTIVE_RESERVATION_INDEX = "uq_reservations_sale_id_user_id_active"


class ReserveRefusal(StrEnum):
    """Почему бронь не выдана."""

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


SELECT_SALE_SQL = text("""
    select quantity, sold, held, start_at, end_at, closed_at, now() as server_time
    from sales
    where id = :sale_id
""")

# Абсолютное значение, посчитанное в Python, — именно это и ломает инвариант.
UPDATE_HELD_SQL = text("update sales set held = :new_held where id = :sale_id")

INSERT_RESERVATION_SQL = text("""
    insert into reservations (sale_id, user_id, status, expires_at)
    values (:sale_id, :user_id, :status, now() + make_interval(mins => :ttl_minutes))
    returning id, sale_id, status, expires_at, created_at
""")


# =============================================================================
#
#   ВНИМАНИЕ: НАИВНАЯ ВЕРСИЯ С ГОНКОЙ. ТОЛЬКО ДЛЯ ДЕМОНСТРАЦИИ.
#   НА ШАГЕ 2.1 БУДЕТ ЗАМЕНЕНА АТОМАРНЫМ УСЛОВНЫМ UPDATE.
#
#   Нарушает инвариант 1 из CLAUDE.md и ADR-002: остаток читается, решение
#   «есть ли место» принимается в Python, и только потом идёт запись. Между
#   чтением и записью влезает конкурент.
#
#   Два покупателя на распродажу с quantity = 1:
#     оба читают held = 0 → оба считают в Python held = 1 → оба пишут held = 1
#     → обе брони вставляются (покупатели разные, частичный уникальный
#        индекс не мешает)
#     → итог: sales.held = 1, а активных броней 2. Товар продан дважды.
#
#   CHECK (sold + held <= quantity) здесь НЕ спасает: запись held = 1
#   абсолютно легальна (0 + 1 <= 1). Последняя линия защиты бессильна именно
#   потому, что код пишет посчитанное значение, а не инкремент. Расходятся
#   sales.held и число строк в reservations.
#
#   Воспроизводится при NAIVE_RESERVE_DELAY_MS > 0 (см. DEVLOG, шаг 2.0).
#
# =============================================================================
async def create_reservation_naive(
    session: AsyncSession,
    *,
    sale_id: UUID,
    user_id: UUID,
) -> ReservationView:
    sale = (await session.execute(SELECT_SALE_SQL, {"sale_id": sale_id})).one_or_none()
    if sale is None:
        raise ReserveError(ReserveRefusal.SALE_NOT_FOUND)

    # Окно проверяется по серверному времени (инвариант 2), но — в Python,
    # то есть по снимку, который к моменту записи уже может быть неверным.
    if sale.server_time < sale.start_at:
        raise ReserveError(ReserveRefusal.NOT_STARTED)
    if sale.closed_at is not None or sale.server_time >= sale.end_at:
        raise ReserveError(ReserveRefusal.ENDED)
    if sale.sold + sale.held >= sale.quantity:
        raise ReserveError(ReserveRefusal.SOLD_OUT)

    new_held = sale.held + 1

    # Окно гонки. При задержке 0 оно всё равно есть — просто короче.
    delay_ms = get_settings().naive_reserve_delay_ms
    if delay_ms > 0:
        logger.warning(
            "НАИВНЫЙ резерв: пауза %d мс между чтением и записью (sale_id=%s)",
            delay_ms,
            sale_id,
        )
        await asyncio.sleep(delay_ms / 1000)

    await session.execute(UPDATE_HELD_SQL, {"sale_id": sale_id, "new_held": new_held})

    try:
        row = (
            await session.execute(
                INSERT_RESERVATION_SQL,
                {
                    "sale_id": sale_id,
                    "user_id": user_id,
                    "status": ReservationStatus.HELD.value,
                    "ttl_minutes": RESERVATION_TTL_MINUTES,
                },
            )
        ).one()
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        # Ловим только «уже есть активная бронь». Всё остальное (включая
        # ck_sales_not_oversold) намеренно не маскируем: это симптом бага,
        # который шаг 2.0 и должен показать.
        if ACTIVE_RESERVATION_INDEX in str(error):
            raise ReserveError(ReserveRefusal.ALREADY_RESERVED) from error
        raise

    return ReservationView(
        id=row.id,
        sale_id=row.sale_id,
        status=row.status,
        expires_at=row.expires_at,
        created_at=row.created_at,
    )
