"""Сверка зависших платежей (ADR-023).

Вебхук может потеряться — у заглушки повторы не бесконечны (ADR-021), да и
ответ на создание платежа мог не дойти. Поэтому backend сам доспрашивает
провайдера о платежах, по которым результата нет.

Переспрос делается тем же `POST /payments` с тем же `idempotency_key`: известный
платёж вернётся с текущим статусом, а неизвестный (создание не дошло) будет
создан сейчас. Одна операция закрывает оба случая.

Сетевые вызовы — вне транзакций. В транзакциях только отметка `last_checked_at`
и `apply_payment_result`.
"""

import asyncio
import logging
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.models import PaymentStatus
from app.services.payments.flow import (
    PaymentRecord,
    apply_payment_result,
    record_provider_payment_id,
)
from app.services.payments.provider import (
    PaymentProvider,
    ProviderUnavailable,
    callback_url,
)

logger = logging.getLogger(__name__)

# Статусы провайдера, которые означают результат.
PROVIDER_RESULTS = {
    "succeeded": PaymentStatus.SUCCEEDED,
    "declined": PaymentStatus.DECLINED,
}

# ВАЖНО, явное исключение из инварианта 10 (см. CLAUDE.md и ADR-023):
# эта транзакция блокирует только `payments` и не берёт `sales` первой.
# Так можно потому, что:
#   1) затронута ровно одна таблица — цикла ожиданий не образуется;
#   2) SKIP LOCKED не ждёт занятых строк, а пропускает их;
#   3) транзакция немедленно коммитится, до любых сетевых вызовов.
# Добавлять сюда другие таблицы нельзя: это сразу вернёт риск дедлока.
#
# SKIP LOCKED заодно «занимает» платёж: второй экземпляр backend пропустит
# занятые строки и не станет дёргать провайдера по тому же платежу. Это
# оптимизация, а не защита — корректность держит условный UPDATE
# в apply_payment_result.
CLAIM_CANDIDATES_SQL = text("""
    with candidates as (
        select p.id
        from payments p
        where p.status = 'pending'
          -- Свежим платежам даём шанс получить вебхук.
          and p.created_at <= now() - make_interval(secs => :grace_seconds)
          -- Нарастающий интервал: base * 2^попытки, но не больше потолка.
          and (
                p.last_checked_at is null
                or p.last_checked_at <= now() - make_interval(secs => least(
                       :backoff_base_seconds * power(2, p.reconcile_attempts),
                       :backoff_max_seconds
                   ))
          )
        order by p.last_checked_at nulls first
        limit :batch_size
        for update of p skip locked
    )
    update payments p
    set last_checked_at = now(),
        reconcile_attempts = p.reconcile_attempts + 1,
        updated_at = now()
    where p.id in (select id from candidates)
    returning p.id, p.idempotency_key, p.provider_payment_id
""")

# Контекст платежа читается отдельным запросом, уже без блокировок: он нужен
# только чтобы применить результат, а решение всё равно примет условный UPDATE.
PAYMENT_CONTEXT_SQL = text("""
    select
        p.id, p.order_id, p.status, p.idempotency_key, p.provider_payment_id,
        o.reservation_id, o.sale_id, o.amount_minor, o.currency
    from payments p join orders o on o.id = p.order_id
    where p.id = any(:payment_ids)
""")


@dataclass(frozen=True)
class Candidate:
    payment: PaymentRecord
    amount_minor: int
    currency: str


@dataclass(frozen=True)
class ReconcileReport:
    checked: int = 0
    settled: int = 0
    still_pending: int = 0
    unavailable: int = 0

    @property
    def is_empty(self) -> bool:
        return self.checked == 0


async def claim_candidates(session: AsyncSession) -> list[Candidate]:
    """Занять пачку платежей и сразу отметить их проверенными.

    Отметка ставится до сетевого вызова намеренно: если провайдер не ответит,
    платёж всё равно не будет опрошен повторно сию же секунду.
    """
    settings = get_settings()
    claimed = (
        await session.execute(
            CLAIM_CANDIDATES_SQL,
            {
                "grace_seconds": settings.reconcile_grace_seconds,
                "backoff_base_seconds": settings.reconcile_backoff_base_seconds,
                "backoff_max_seconds": settings.reconcile_backoff_max_seconds,
                "batch_size": settings.reconcile_batch_size,
            },
        )
    ).all()
    payment_ids = [row.id for row in claimed]
    if not payment_ids:
        await session.rollback()
        return []

    rows = (await session.execute(PAYMENT_CONTEXT_SQL, {"payment_ids": payment_ids})).all()
    # Короткая транзакция закрывается здесь — дальше только сеть.
    await session.commit()

    return [
        Candidate(
            payment=PaymentRecord(
                id=row.id,
                order_id=row.order_id,
                reservation_id=row.reservation_id,
                sale_id=row.sale_id,
                status=row.status,
                idempotency_key=row.idempotency_key,
                provider_payment_id=row.provider_payment_id,
            ),
            amount_minor=row.amount_minor,
            currency=row.currency,
        )
        for row in rows
    ]


async def reconcile_one(
    session: AsyncSession, candidate: Candidate, provider: PaymentProvider
) -> str:
    """Доспросить провайдера об одном платеже. Возвращает исход для отчёта.

    Переспрос — тот же `POST /payments` с тем же ключом: если создание платежа
    не дошло, он создастся сейчас; если дошло, вернётся текущий статус
    (проверено на заглушке).
    """
    try:
        answer = await provider.create_payment(
            idempotency_key=candidate.payment.idempotency_key,
            amount_minor=candidate.amount_minor,
            currency=candidate.currency,
            callback_url=callback_url(),
        )
    except ProviderUnavailable as error:
        logger.warning(
            "сверка: провайдер недоступен по платежу %s: %s", candidate.payment.id, error
        )
        return "unavailable"

    if candidate.payment.provider_payment_id is None:
        await record_provider_payment_id(session, candidate.payment.id, answer.provider_payment_id)

    result = PROVIDER_RESULTS.get(answer.status)
    if result is None:
        # Всё ещё pending — например, режим hang. Ждём сколько угодно:
        # заказ не отменяем, следующая проверка будет позже (инвариант 4).
        return "pending"

    applied = await apply_payment_result(
        session,
        candidate.payment,
        result,
        provider_payment_id=answer.provider_payment_id,
    )
    if applied:
        logger.info(
            "сверка завершила платёж %s со статусом %s",
            candidate.payment.id,
            answer.status,
        )
    return "settled" if applied else "pending"


async def reconcile_once(
    session: AsyncSession, provider: PaymentProvider | None = None
) -> ReconcileReport:
    """Одна итерация сверки. Отдельно от цикла — чтобы вызывать из тестов."""
    if provider is None:
        from app.services.payments import get_payment_provider

        provider = get_payment_provider()

    candidates = await claim_candidates(session)
    if not candidates:
        return ReconcileReport()

    settled = pending = unavailable = 0
    for candidate in candidates:
        outcome = await reconcile_one(session, candidate, provider)
        if outcome == "settled":
            settled += 1
        elif outcome == "unavailable":
            unavailable += 1
        else:
            pending += 1

    report = ReconcileReport(
        checked=len(candidates),
        settled=settled,
        still_pending=pending,
        unavailable=unavailable,
    )
    logger.info(
        "сверка: проверено %d, завершено %d, ещё ждут %d, провайдер недоступен %d",
        report.checked,
        report.settled,
        report.still_pending,
        report.unavailable,
    )
    return report


async def run_reconciler() -> None:
    """Фоновый цикл: первый проход сразу, дальше по интервалу (ADR-010).

    Ошибка итерации и недоступность провайдера логируются и не убивают цикл.
    """
    interval = get_settings().reconcile_interval_seconds
    logger.info("сверка платежей запущена, интервал %d с", interval)

    while True:
        try:
            async with get_sessionmaker()() as session:
                await reconcile_once(session)
        except asyncio.CancelledError:
            logger.info("сверка платежей остановлена")
            raise
        except Exception:
            logger.exception("итерация сверки упала, цикл продолжается")

        await asyncio.sleep(interval)


async def pending_payment_ids(session: AsyncSession) -> list[UUID]:
    """Платежи без результата — для наблюдения и тестов."""
    result = await session.scalars(
        text("select id from payments where status = 'pending' order by created_at")
    )
    return list(result.all())
