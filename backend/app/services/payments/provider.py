"""Клиент платёжного провайдера.

Заглушка — чужая система (ADR-021), поэтому общение с ней спрятано за
интерфейсом: тесты подменяют его фейком и не поднимают настоящий сервис.
"""

import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

WEBHOOK_PATH = "/api/payments/webhook"


class ProviderUnavailable(Exception):
    """Провайдер не ответил: сеть, таймаут или ошибка на его стороне.

    Это не повод отдавать 500: заказ уже создан, его догонит сверка.
    """


@dataclass(frozen=True)
class ProviderPayment:
    """Что ответил провайдер на создание платежа."""

    provider_payment_id: str
    status: str


class PaymentProvider(Protocol):
    async def create_payment(
        self,
        *,
        idempotency_key: str,
        amount_minor: int,
        currency: str,
        callback_url: str,
    ) -> ProviderPayment: ...

    async def get_payment(self, provider_payment_id: str) -> ProviderPayment | None: ...


class HttpPaymentProvider:
    """Настоящий клиент заглушки."""

    async def create_payment(
        self,
        *,
        idempotency_key: str,
        amount_minor: int,
        currency: str,
        callback_url: str,
    ) -> ProviderPayment:
        settings = get_settings()
        try:
            async with httpx.AsyncClient(
                base_url=settings.payment_stub_url,
                timeout=settings.payment_request_timeout_seconds,
            ) as client:
                response = await client.post(
                    "/payments",
                    json={
                        "idempotency_key": idempotency_key,
                        "amount_minor": amount_minor,
                        "currency": currency,
                        "callback_url": callback_url,
                    },
                )
        except Exception as error:
            raise ProviderUnavailable(str(error)) from error

        # 202 — платёж создан, 200 — тот же ключ уже был. Оба нам подходят:
        # ключ детерминированный, поэтому повтор безопасен.
        if response.status_code not in (200, 202):
            raise ProviderUnavailable(
                f"провайдер ответил {response.status_code}: {response.text[:200]}"
            )

        body = response.json()
        return ProviderPayment(provider_payment_id=body["payment_id"], status=body["status"])

    async def get_payment(self, provider_payment_id: str) -> ProviderPayment | None:
        """Спросить статус напрямую — понадобится сверке зависших платежей."""
        settings = get_settings()
        try:
            async with httpx.AsyncClient(
                base_url=settings.payment_stub_url,
                timeout=settings.payment_request_timeout_seconds,
            ) as client:
                response = await client.get(f"/payments/{provider_payment_id}")
        except Exception as error:
            raise ProviderUnavailable(str(error)) from error

        if response.status_code == 404:
            return None
        if not response.is_success:
            raise ProviderUnavailable(f"провайдер ответил {response.status_code}")

        body = response.json()
        return ProviderPayment(provider_payment_id=body["payment_id"], status=body["status"])


def callback_url() -> str:
    """Адрес, на который провайдер пришлёт результат."""
    base = get_settings().payment_callback_base_url.rstrip("/")
    return f"{base}{WEBHOOK_PATH}"
