"""Оплата брони.

Точка подмены провайдера живёт здесь: тесты ставят фейк через
`set_payment_provider`, не поднимая настоящую заглушку (ADR-021).
"""

from app.services.payments.flow import (
    PayError,
    PayRefusal,
    PayResult,
    ProviderState,
    apply_payment_result,
    find_payment_by_idempotency_key,
    start_payment,
)
from app.services.payments.provider import (
    HttpPaymentProvider,
    PaymentProvider,
    ProviderPayment,
    ProviderUnavailable,
    callback_url,
)

_provider: PaymentProvider = HttpPaymentProvider()


def get_payment_provider() -> PaymentProvider:
    return _provider


def set_payment_provider(provider: PaymentProvider) -> PaymentProvider:
    """Подменить провайдера. Возвращает предыдущего — чтобы вернуть обратно."""
    global _provider
    previous = _provider
    _provider = provider
    return previous


__all__ = [
    "HttpPaymentProvider",
    "PayError",
    "PayRefusal",
    "PayResult",
    "PaymentProvider",
    "ProviderPayment",
    "ProviderState",
    "ProviderUnavailable",
    "apply_payment_result",
    "callback_url",
    "find_payment_by_idempotency_key",
    "get_payment_provider",
    "set_payment_provider",
    "start_payment",
]
