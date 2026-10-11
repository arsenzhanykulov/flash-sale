"""Настройки заглушки.

Своя конфигурация, не переиспользует настройки основного сервиса: заглушка
изображает чужую систему (ADR-021).
"""

from enum import StrEnum
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class PaymentMode(StrEnum):
    """Как заглушка отвечает на новые платежи."""

    APPROVE = "approve"
    DECLINE = "decline"
    # Платёж навсегда остаётся pending — отпускается только вручную.
    HANG = "hang"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore")

    # Файл SQLite. В контейнере лежит в volume, поэтому состояние переживает
    # перезапуск.
    database_path: str = "/data/payments.db"

    # Режим и задержка по умолчанию. На лету меняются через POST /control/mode
    # и хранятся в БД: переключение не должно теряться при перезапуске.
    payment_mode: PaymentMode = PaymentMode.APPROVE
    payment_delay_ms: int = Field(default=0, ge=0)
    # Присылать результат дважды — чтобы проверить идемпотентность получателя.
    duplicate_webhooks: bool = False

    # Общий секрет для подписи вебхуков. Обе стороны берут его из окружения.
    webhook_secret: str = "dev-only-webhook-secret"
    webhook_timeout_seconds: float = Field(default=5.0, gt=0)
    webhook_max_attempts: int = Field(default=5, ge=1)
    # Пауза перед первым повтором; дальше удваивается.
    webhook_retry_base_ms: int = Field(default=500, ge=0)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
