"""Отправка результата платежа на callback_url.

Подпись HMAC-SHA256 общим секретом считается от **тех самых байтов**, которые
уходят в теле: пересчёт из разобранного JSON дал бы другой порядок ключей
и подпись не сошлась бы на принимающей стороне.
"""

import asyncio
import hashlib
import hmac
import json
import logging

import httpx

from app.config import get_settings
from app.storage import Payment, Storage

logger = logging.getLogger(__name__)

SIGNATURE_HEADER = "X-Signature"


def http_client(**kwargs) -> httpx.AsyncClient:
    """Клиент для отправки вебхуков.

    Отдельная функция, чтобы тесты подменяли транспорт только здесь: подмена
    самого `httpx.AsyncClient` задела бы и клиент, которым тесты обращаются
    к заглушке.
    """
    return httpx.AsyncClient(**kwargs)


def sign(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def webhook_body(payment: Payment) -> bytes:
    """Тело вебхука. separators без пробелов — чтобы байты были предсказуемы."""
    return json.dumps(
        {
            "payment_id": payment.id,
            "idempotency_key": payment.idempotency_key,
            "status": payment.status.value,
        },
        separators=(",", ":"),
    ).encode()


async def deliver(payment: Payment, storage: Storage) -> bool:
    """Доставить вебхук, повторяя при неудаче с нарастающей паузой.

    Возвращает True, если получатель ответил 2xx. Исчерпание попыток — не
    ошибка заглушки: потерянный вебхук закрывается сверкой статуса на стороне
    backend (ADR-021).
    """
    settings = get_settings()
    body = webhook_body(payment)
    headers = {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: sign(body, settings.webhook_secret),
    }

    for attempt in range(settings.webhook_max_attempts):
        delivered = False
        try:
            async with http_client(timeout=settings.webhook_timeout_seconds) as client:
                response = await client.post(payment.callback_url, content=body, headers=headers)
            delivered = response.is_success
            if not delivered:
                logger.warning(
                    "вебхук %s: получатель ответил %s (попытка %d)",
                    payment.id,
                    response.status_code,
                    attempt + 1,
                )
        except Exception as error:
            logger.warning(
                "вебхук %s: получатель недоступен (попытка %d): %s",
                payment.id,
                attempt + 1,
                error,
            )

        await storage.count_webhook_attempt(payment.id, delivered=delivered)
        if delivered:
            logger.info("вебхук %s доставлен со статусом %s", payment.id, payment.status.value)
            return True

        last_attempt = attempt == settings.webhook_max_attempts - 1
        if not last_attempt:
            await asyncio.sleep(settings.webhook_retry_base_ms * (2**attempt) / 1000)

    logger.error(
        "вебхук %s не доставлен за %d попыток — backend должен сверить статус сам",
        payment.id,
        settings.webhook_max_attempts,
    )
    return False


async def deliver_result(payment: Payment, storage: Storage) -> None:
    """Отправить результат, при включённом флаге — дважды.

    Дубль нужен, чтобы проверить идемпотентность приёма на стороне backend:
    повторный вебхук не должен менять состояние второй раз.
    """
    mode = await storage.get_mode()
    await deliver(payment, storage)
    if mode.duplicate_webhooks:
        logger.info("вебхук %s: отправляем дубль (duplicate_webhooks)", payment.id)
        await deliver(payment, storage)
