"""Оплата брони: начало платежа и применение результата.

Инвариант 10 — порядок захвата блокировок `sales` → `reservations` →
`orders` → `payments`. Он соблюдается в обеих операциях: иначе встречный
резерв, отмена или свипер дают дедлок (ADR-018).

Инвариант 4: бронь в `paying` не истекает. Поэтому сетевой вызов провайдера
делается **вне транзакции** — блокировки БД не ждут чужую систему.

Инвариант 5: один заказ и один платёж на бронь — держат уникальные индексы.
Инвариант 6: письмо кладётся в `outbox` в той же транзакции, что и смена
статуса, с уникальным `dedup_key` (ADR-004).
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OrderStatus, PaymentStatus, ReservationStatus
from app.services.locks import lock_sale

logger = logging.getLogger(__name__)


# Ключ идемпотентности выводится из брони, а не случайный: повторный «Оплатить»
# и повтор после сетевого сбоя приходят к провайдеру с тем же ключом, и он
# не создаёт второй платёж (ADR-021).
def idempotency_key_for(reservation_id: UUID) -> str:
    return f"pay:{reservation_id}"


# dedup_key письма — по заказу: повторный вебхук не добавит второе письмо.
def order_paid_dedup_key(order_id: UUID) -> str:
    return f"order_paid:{order_id}"


class PayRefusal(StrEnum):
    RESERVATION_NOT_FOUND = "reservation_not_found"
    RESERVATION_EXPIRED = "reservation_expired"
    RESERVATION_CANCELLED = "reservation_cancelled"
    PAYMENT_FAILED = "payment_failed"
    SALE_NOT_PAYABLE = "sale_not_payable"


class PayError(Exception):
    def __init__(self, reason: PayRefusal) -> None:
        super().__init__(reason.value)
        self.reason = reason


class ProviderState(StrEnum):
    """Что удалось сделать с провайдером."""

    ACCEPTED = "accepted"
    # Провайдер не ответил. Заказ создан, догонит сверка.
    AWAITING_PROVIDER = "awaiting_provider"


@dataclass(frozen=True)
class PayResult:
    order_id: UUID
    payment_id: UUID
    order_status: str
    payment_status: str
    amount_minor: int
    currency: str
    idempotency_key: str
    provider_state: ProviderState
    # True, если платёж уже был начат раньше (двойной клик).
    already_started: bool


# --- начало оплаты -----------------------------------------------------------

# sale_id брони неизменяем, поэтому читается без блокировки: нужен только
# чтобы знать, какую строку sales захватывать первой (инвариант 10).
RESERVATION_SALE_SQL = text("""
    select r.sale_id, s.price_minor, s.currency
    from reservations r join sales s on s.id = r.sale_id
    where r.id = :reservation_id and r.user_id = :user_id
""")

# Решение принимает это условие: `status = 'held'` и живой срок проверяются
# тем же оператором, который и записывает.
START_PAYING_SQL = text(f"""
    update reservations
    set status = '{ReservationStatus.PAYING.value}', updated_at = now()
    where id = :reservation_id
      and user_id = :user_id
      and status = '{ReservationStatus.HELD.value}'
      and expires_at > now()
    returning sale_id
""")

INSERT_ORDER_SQL = text(f"""
    insert into orders (reservation_id, user_id, sale_id, amount_minor, currency, status)
    values (:reservation_id, :user_id, :sale_id, :amount_minor, :currency,
            '{OrderStatus.PENDING_PAYMENT.value}')
    returning id
""")

INSERT_PAYMENT_SQL = text(f"""
    insert into payments (order_id, idempotency_key, status)
    values (:order_id, :idempotency_key, '{PaymentStatus.PENDING.value}')
    returning id
""")

# Для ответа на повторный «Оплатить» и для выяснения причины отказа.
EXISTING_ORDER_SQL = text("""
    select
        o.id as order_id,
        o.status as order_status,
        o.amount_minor,
        o.currency,
        p.id as payment_id,
        p.status as payment_status,
        p.idempotency_key,
        p.provider_payment_id
    from orders o join payments p on p.order_id = o.id
    where o.reservation_id = :reservation_id
""")

RESERVATION_STATE_SQL = text("""
    select status, expires_at <= now() as expired
    from reservations where id = :reservation_id and user_id = :user_id
""")


async def start_payment(
    session: AsyncSession,
    *,
    reservation_id: UUID,
    user_id: UUID,
) -> tuple[PayResult, str, int, str]:
    """Перевести бронь в `paying` и создать заказ с платежом.

    Возвращает результат и данные для вызова провайдера. Сам вызов делает
    слой выше — **вне транзакции**: сетевой запрос не должен держать
    блокировки БД, пока бронь в `paying` всё равно не истекает (инвариант 4).
    """
    row = (
        await session.execute(
            RESERVATION_SALE_SQL, {"reservation_id": reservation_id, "user_id": user_id}
        )
    ).one_or_none()
    if row is None:
        # Нет брони или она чужая — не раскрываем, какая из причин.
        await session.rollback()
        raise PayError(PayRefusal.RESERVATION_NOT_FOUND)

    # Инвариант 10: sales раньше reservations.
    await lock_sale(session, row.sale_id)

    started = await session.scalar(
        START_PAYING_SQL, {"reservation_id": reservation_id, "user_id": user_id}
    )
    if started is None:
        return await _resume_or_refuse(session, reservation_id, user_id)

    idempotency_key = idempotency_key_for(reservation_id)
    try:
        order_id = await session.scalar(
            INSERT_ORDER_SQL,
            {
                "reservation_id": reservation_id,
                "user_id": user_id,
                "sale_id": row.sale_id,
                # Сумма фиксируется на момент заказа: позднее изменение цены
                # распродажи на него не влияет.
                "amount_minor": row.price_minor,
                "currency": row.currency,
            },
        )
        payment_id = await session.scalar(
            INSERT_PAYMENT_SQL,
            {"order_id": order_id, "idempotency_key": idempotency_key},
        )
        await session.commit()
    except IntegrityError:
        # Один заказ и один платёж на бронь держат уникальные индексы
        # (инвариант 5). Сюда попадаем, если заказ успели создать параллельно.
        await session.rollback()
        return await _resume_or_refuse(session, reservation_id, user_id)

    result = PayResult(
        order_id=order_id,
        payment_id=payment_id,
        order_status=OrderStatus.PENDING_PAYMENT.value,
        payment_status=PaymentStatus.PENDING.value,
        amount_minor=row.price_minor,
        currency=row.currency,
        idempotency_key=idempotency_key,
        provider_state=ProviderState.ACCEPTED,
        already_started=False,
    )
    return result, idempotency_key, row.price_minor, row.currency


async def _resume_or_refuse(
    session: AsyncSession, reservation_id: UUID, user_id: UUID
) -> tuple[PayResult, str, int, str]:
    """Условный UPDATE строку не дал — разобраться, почему.

    Запрос только для ответа: решение уже принято оператором выше.
    """
    state = (
        await session.execute(
            RESERVATION_STATE_SQL, {"reservation_id": reservation_id, "user_id": user_id}
        )
    ).one_or_none()
    if state is None:
        await session.rollback()
        raise PayError(PayRefusal.RESERVATION_NOT_FOUND)

    status = ReservationStatus(state.status)

    if status in (ReservationStatus.PAYING, ReservationStatus.PAID):
        # Двойной клик: заказ уже есть, второй не создаём. Провайдеру уйдёт
        # запрос с тем же ключом — он вернёт тот же платёж.
        existing = (
            await session.execute(EXISTING_ORDER_SQL, {"reservation_id": reservation_id})
        ).one_or_none()
        await session.rollback()
        if existing is None:
            # Бронь в paying без заказа — состояние, которого быть не должно.
            logger.error("бронь %s в статусе %s без заказа", reservation_id, status.value)
            raise PayError(PayRefusal.SALE_NOT_PAYABLE)

        result = PayResult(
            order_id=existing.order_id,
            payment_id=existing.payment_id,
            order_status=existing.order_status,
            payment_status=existing.payment_status,
            amount_minor=existing.amount_minor,
            currency=existing.currency,
            idempotency_key=existing.idempotency_key,
            provider_state=ProviderState.ACCEPTED,
            already_started=True,
        )
        return result, existing.idempotency_key, existing.amount_minor, existing.currency

    await session.rollback()
    if status is ReservationStatus.HELD and state.expired:
        # Срок истёк, свипер ещё не дошёл — для клиента это то же, что expired.
        raise PayError(PayRefusal.RESERVATION_EXPIRED)
    if status is ReservationStatus.EXPIRED:
        raise PayError(PayRefusal.RESERVATION_EXPIRED)
    if status is ReservationStatus.CANCELLED:
        raise PayError(PayRefusal.RESERVATION_CANCELLED)
    if status is ReservationStatus.FAILED:
        raise PayError(PayRefusal.PAYMENT_FAILED)

    logger.warning("бронь %s: оплата невозможна при статусе %s", reservation_id, status.value)
    raise PayError(PayRefusal.SALE_NOT_PAYABLE)


# --- применение результата ---------------------------------------------------


@dataclass(frozen=True)
class PaymentRecord:
    """Платёж вместе с контекстом, нужным для применения результата."""

    id: UUID
    order_id: UUID
    reservation_id: UUID
    sale_id: UUID
    status: str
    idempotency_key: str
    provider_payment_id: str | None


PAYMENT_BY_KEY_SQL = text("""
    select
        p.id, p.order_id, p.status, p.idempotency_key, p.provider_payment_id,
        o.reservation_id, o.sale_id
    from payments p join orders o on o.id = p.order_id
    where p.idempotency_key = :idempotency_key
""")


async def find_payment_by_idempotency_key(
    session: AsyncSession, idempotency_key: str
) -> PaymentRecord | None:
    """Найти платёж по НАШЕМУ ключу, а не по id провайдера.

    Ответ на создание платежа мог не дойти, и тогда `provider_payment_id`
    у нас пустой — по нему вебхук не нашёл бы платёж. Ключ же известен
    заранее: он выводится из брони.
    """
    row = (
        await session.execute(PAYMENT_BY_KEY_SQL, {"idempotency_key": idempotency_key})
    ).one_or_none()
    if row is None:
        return None
    return PaymentRecord(
        id=row.id,
        order_id=row.order_id,
        reservation_id=row.reservation_id,
        sale_id=row.sale_id,
        status=row.status,
        idempotency_key=row.idempotency_key,
        provider_payment_id=row.provider_payment_id,
    )


# Решение принимает это условие: два одновременных вебхука — второй получит
# 0 строк и ничего не изменит, поэтому sold + 1 случится один раз.
SETTLE_PAYMENT_SQL = text(f"""
    update payments
    set status = :status,
        provider_payment_id = coalesce(provider_payment_id, :provider_payment_id),
        last_checked_at = now(),
        updated_at = now()
    where id = :payment_id and status = '{PaymentStatus.PENDING.value}'
    returning id
""")

MARK_ORDER_PAID_SQL = text(f"""
    update orders set status = '{OrderStatus.PAID.value}', paid_at = now()
    where id = :order_id
""")

MARK_ORDER_FAILED_SQL = text(f"""
    update orders set status = '{OrderStatus.FAILED.value}' where id = :order_id
""")

MARK_RESERVATION_SQL = text("""
    update reservations set status = :status, updated_at = now()
    where id = :reservation_id
""")

# paid: единица перестаёт быть удержанной и становится проданной.
SOLD_SQL = text("update sales set held = held - 1, sold = sold + 1 where id = :sale_id")
# declined: единица возвращается на витрину.
RELEASE_SQL = text("update sales set held = held - 1 where id = :sale_id")

# Инвариант 6: письмо пишется в той же транзакции, что и смена статуса.
# ON CONFLICT DO NOTHING + уникальный dedup_key = не более одного письма
# на событие, даже если вебхук придёт дважды (ADR-004).
INSERT_OUTBOX_SQL = text("""
    insert into outbox (kind, dedup_key, recipient, payload, status)
    select
        'order_paid',
        :dedup_key,
        u.email,
        jsonb_build_object(
            'order_id', o.id::text,
            'sale_id', o.sale_id::text,
            'product_name', p.name,
            'amount_minor', o.amount_minor,
            'currency', o.currency
        ),
        'pending'
    from orders o
    join users u on u.id = o.user_id
    join sales s on s.id = o.sale_id
    join products p on p.id = s.product_id
    where o.id = :order_id
    on conflict (dedup_key) do nothing
""")


async def apply_payment_result(
    session: AsyncSession,
    payment: PaymentRecord,
    status: PaymentStatus,
    *,
    provider_payment_id: str | None = None,
) -> bool:
    """Применить результат платежа. True — применён сейчас, False — уже был.

    Вызывается и из вебхука, и из сверки зависших платежей (шаг 9), поэтому
    живёт отдельной функцией. Порядок блокировок — инвариант 10.
    """
    if status not in (PaymentStatus.SUCCEEDED, PaymentStatus.DECLINED):
        raise ValueError(f"нельзя применить статус {status}")

    # Инвариант 10: sales первой, затем reservations, orders, payments.
    await lock_sale(session, payment.sale_id)

    settled = await session.scalar(
        SETTLE_PAYMENT_SQL,
        {
            "payment_id": payment.id,
            "status": status.value,
            "provider_payment_id": provider_payment_id,
        },
    )
    if settled is None:
        # Уже обработан: повторный вебхук ничего не меняет.
        await session.rollback()
        return False

    if status is PaymentStatus.SUCCEEDED:
        await session.execute(MARK_ORDER_PAID_SQL, {"order_id": payment.order_id})
        await session.execute(
            MARK_RESERVATION_SQL,
            {"reservation_id": payment.reservation_id, "status": ReservationStatus.PAID.value},
        )
        await session.execute(SOLD_SQL, {"sale_id": payment.sale_id})
        await session.execute(
            INSERT_OUTBOX_SQL,
            {
                "order_id": payment.order_id,
                "dedup_key": order_paid_dedup_key(payment.order_id),
            },
        )
    else:
        await session.execute(MARK_ORDER_FAILED_SQL, {"order_id": payment.order_id})
        await session.execute(
            MARK_RESERVATION_SQL,
            {
                "reservation_id": payment.reservation_id,
                "status": ReservationStatus.FAILED.value,
            },
        )
        await session.execute(RELEASE_SQL, {"sale_id": payment.sale_id})

    await session.commit()
    logger.info("платёж %s: применён результат %s", payment.id, status.value)
    return True


async def record_provider_payment_id(
    session: AsyncSession, payment_id: UUID, provider_payment_id: str
) -> None:
    """Дозаписать id платежа у провайдера, если его ещё нет."""
    await session.execute(
        text("""
            update payments
            set provider_payment_id = :provider_payment_id, updated_at = now()
            where id = :payment_id and provider_payment_id is null
        """),
        {"payment_id": payment_id, "provider_payment_id": provider_payment_id},
    )
    await session.commit()


@dataclass(frozen=True)
class OrderSummary:
    order_id: UUID
    payment_id: UUID
    order_status: str
    payment_status: str
    amount_minor: int
    currency: str
    paid_at: datetime | None
