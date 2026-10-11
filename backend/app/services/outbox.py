"""Отправка писем из outbox (ADR-004, ADR-024).

Инвариант 6: письмо пишется в `outbox` в той же транзакции, что и смена
статуса, — это уже сделано на шаге 8. Здесь — доставка.

**Граница гарантии.** Между «SMTP принял письмо» и «отметили sent» есть окно:
это две системы без общей транзакции. Падение ровно в нём даёт либо потерю,
либо дубль — третьего не бывает. Выбран **дубль**: потерянное письмо об оплате
означает, что покупатель заплатил и не узнал об этом, а второе такое же письмо
безвредно. `Message-ID` постоянный (выводится из `dedup_key`), поэтому повтор
опознаётся и подавляется на стороне получателя.
То есть доставка — at-least-once с дедупликацией по `Message-ID`, а не
exactly-once. Запись в `outbox` при этом строго одна (уникальный `dedup_key`).

Порядок работы: занять пачку (одна таблица, `SKIP LOCKED`, немедленный коммит —
исключение из инварианта 10 по образцу ADR-023) → отправить **вне транзакции**
→ короткая транзакция с отметкой.
"""

import asyncio
import logging
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.services.mail import (
    MailDeliveryError,
    SmtpMailer,
    UnknownMailKind,
    build_message,
)

logger = logging.getLogger(__name__)

# Исключение из инварианта 10 (CLAUDE.md, ADR-023): блокируется ровно одна
# таблица, SKIP LOCKED не ждёт, транзакция коммитится до отправки письма.
# Добавлять сюда другие таблицы нельзя.
#
# locked_until <= now() возвращает в работу строки, занятые упавшим процессом:
# без этого письмо осталось бы занятым навсегда.
CLAIM_BATCH_SQL = text("""
    with claimed as (
        select id from outbox
        where status = 'pending'
          and next_attempt_at <= now()
          and (locked_until is null or locked_until <= now())
        order by next_attempt_at
        limit :batch_size
        for update skip locked
    )
    update outbox o
    set locked_until = now() + make_interval(secs => :lease_seconds)
    where o.id in (select id from claimed)
    returning o.id, o.kind, o.dedup_key, o.recipient, o.payload, o.attempts
""")

MARK_SENT_SQL = text("""
    update outbox
    set status = 'sent', sent_at = now(), locked_until = null, last_error = null
    where id = :id and status = 'pending'
""")

# Нарастающая пауза: base * 2^attempts. Аренду снимаем — строка снова свободна,
# но раньше next_attempt_at её никто не возьмёт.
SCHEDULE_RETRY_SQL = text("""
    update outbox
    set attempts = attempts + 1,
        next_attempt_at = now() + make_interval(
            secs => :retry_base_seconds * power(2, attempts)
        ),
        locked_until = null,
        last_error = :last_error
    where id = :id and status = 'pending'
""")

MARK_FAILED_SQL = text("""
    update outbox
    set status = 'failed', attempts = attempts + 1, locked_until = null,
        last_error = :last_error
    where id = :id and status = 'pending'
""")


@dataclass(frozen=True)
class OutboxLetter:
    id: UUID
    kind: str
    dedup_key: str
    recipient: str
    payload: dict
    attempts: int


@dataclass(frozen=True)
class SendReport:
    claimed: int = 0
    sent: int = 0
    retried: int = 0
    failed: int = 0


async def claim_letters(session: AsyncSession) -> list[OutboxLetter]:
    """Занять пачку писем и сразу закоммитить: дальше только сеть."""
    settings = get_settings()
    rows = (
        await session.execute(
            CLAIM_BATCH_SQL,
            {
                "batch_size": settings.outbox_batch_size,
                "lease_seconds": settings.outbox_lease_seconds,
            },
        )
    ).all()
    await session.commit()
    return [
        OutboxLetter(
            id=row.id,
            kind=row.kind,
            dedup_key=row.dedup_key,
            recipient=row.recipient,
            payload=row.payload,
            attempts=row.attempts,
        )
        for row in rows
    ]


async def mark_sent(session: AsyncSession, letter_id: UUID) -> None:
    await session.execute(MARK_SENT_SQL, {"id": letter_id})
    await session.commit()


async def schedule_retry(session: AsyncSession, letter: OutboxLetter, error: str) -> bool:
    """Запланировать повтор или признать письмо недоставленным.

    Возвращает True, если попытки исчерпаны и письмо помечено failed.
    """
    settings = get_settings()
    # attempts в строке ещё не увеличен — это сделает сам UPDATE.
    exhausted = letter.attempts + 1 >= settings.outbox_max_attempts
    if exhausted:
        await session.execute(MARK_FAILED_SQL, {"id": letter.id, "last_error": error[:1000]})
        await session.commit()
        logger.error(
            "письмо %s (%s) не доставлено за %d попыток: %s",
            letter.id,
            letter.dedup_key,
            settings.outbox_max_attempts,
            error,
        )
        return True

    await session.execute(
        SCHEDULE_RETRY_SQL,
        {
            "id": letter.id,
            "retry_base_seconds": settings.outbox_retry_base_seconds,
            "last_error": error[:1000],
        },
    )
    await session.commit()
    logger.warning(
        "письмо %s не отправлено (попытка %d), повторим позже: %s",
        letter.id,
        letter.attempts + 1,
        error,
    )
    return False


async def mark_unsendable(session: AsyncSession, letter: OutboxLetter, error: str) -> None:
    """Письмо нельзя собрать — повторять бессмысленно, сразу failed.

    Неизвестный `kind` или неполный payload: повтор ничего не изменит, но цикл
    должен продолжать работу.
    """
    await session.execute(MARK_FAILED_SQL, {"id": letter.id, "last_error": error[:1000]})
    await session.commit()
    logger.error("письмо %s не может быть собрано: %s", letter.id, error)


async def send_outbox_once(session: AsyncSession, mailer: SmtpMailer | None = None) -> SendReport:
    """Одна итерация отправки. Отдельно от цикла — чтобы звать из тестов."""
    sender = mailer if mailer is not None else SmtpMailer()

    letters = await claim_letters(session)
    if not letters:
        return SendReport()

    sent = retried = failed = 0
    for letter in letters:
        try:
            message = build_message(
                kind=letter.kind,
                dedup_key=letter.dedup_key,
                recipient=letter.recipient,
                payload=letter.payload,
            )
        except UnknownMailKind as error:
            await mark_unsendable(session, letter, str(error))
            failed += 1
            continue

        try:
            # Вне транзакции: окно между отправкой и отметкой минимально —
            # сразу за send идёт mark_sent, без промежуточной работы.
            await sender.send(message)
        except MailDeliveryError as error:
            if await schedule_retry(session, letter, str(error)):
                failed += 1
            else:
                retried += 1
            continue

        await mark_sent(session, letter.id)
        sent += 1
        logger.info("письмо %s отправлено на %s", letter.dedup_key, letter.recipient)

    report = SendReport(claimed=len(letters), sent=sent, retried=retried, failed=failed)
    if report.claimed:
        logger.info(
            "outbox: взято %d, отправлено %d, повтор %d, отказ %d",
            report.claimed,
            report.sent,
            report.retried,
            report.failed,
        )
    return report


async def run_outbox_sender() -> None:
    """Фоновый цикл: первый проход сразу, дальше по интервалу (ADR-010)."""
    interval = get_settings().outbox_interval_seconds
    logger.info("отправка писем запущена, интервал %d с", interval)

    while True:
        try:
            async with get_sessionmaker()() as session:
                await send_outbox_once(session)
        except asyncio.CancelledError:
            logger.info("отправка писем остановлена")
            raise
        except Exception:
            logger.exception("итерация отправки писем упала, цикл продолжается")

        await asyncio.sleep(interval)
