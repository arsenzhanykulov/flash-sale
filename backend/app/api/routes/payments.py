"""Приём результата платежа от провайдера."""

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, HTTPException, Request, status

from app.api.deps import SessionDep
from app.api.schemas.payments import WebhookAck, WebhookPayload
from app.core.config import get_settings
from app.models import PaymentStatus
from app.services.payments import apply_payment_result, find_payment_by_idempotency_key

logger = logging.getLogger(__name__)

router = APIRouter()

SIGNATURE_HEADER = "X-Signature"

# Статусы провайдера → наши. Остальное игнорируем: pending означает,
# что результата пока нет.
PROVIDER_STATUSES = {
    "succeeded": PaymentStatus.SUCCEEDED,
    "declined": PaymentStatus.DECLINED,
}


def signature_matches(body: bytes, header: str | None) -> bool:
    """Подпись считается от СЫРОГО тела.

    Пересчёт из разобранного JSON дал бы другой порядок ключей и не сошёлся бы
    с подписью провайдера. compare_digest — чтобы сравнение не зависело
    от времени.
    """
    if header is None:
        return False
    expected = hmac.new(
        get_settings().payment_webhook_secret.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(header, f"sha256={expected}")


@router.post(
    "/webhook",
    response_model=WebhookAck,
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Подпись неверна или отсутствует"},
        status.HTTP_404_NOT_FOUND: {"description": "Платёж с таким ключом не найден"},
    },
    summary="Результат платежа от провайдера",
)
async def payment_webhook(request: Request, session: SessionDep) -> WebhookAck:
    body = await request.body()

    # Проверяем подпись до разбора тела: неподписанное содержимое вообще
    # не должно попадать в парсер.
    if not signature_matches(body, request.headers.get(SIGNATURE_HEADER)):
        logger.warning("вебхук с неверной подписью отклонён")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Неверная подпись")

    try:
        payload = WebhookPayload.model_validate(json.loads(body))
    except Exception as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Некорректное тело"
        ) from error

    # Ищем по НАШЕМУ ключу: ответ на создание платежа мог не дойти, и тогда
    # provider_payment_id у нас пустой. Ключ известен заранее.
    payment = await find_payment_by_idempotency_key(session, payload.idempotency_key)
    if payment is None:
        logger.warning("вебхук про неизвестный платёж %s", payload.idempotency_key)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Платёж не найден")

    mapped = PROVIDER_STATUSES.get(payload.status)
    if mapped is None:
        # pending или что-то новое: ничего не меняем, но и не ругаемся.
        logger.info("вебхук %s: статус %s без решения", payment.id, payload.status)
        return WebhookAck(applied=False)

    applied = await apply_payment_result(
        session, payment, mapped, provider_payment_id=payload.payment_id
    )
    # 200 в обоих случаях: иначе провайдер продолжит повторять уже
    # обработанный результат.
    return WebhookAck(applied=applied)
