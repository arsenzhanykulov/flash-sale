"""Настройки приложения. Источник — только переменные окружения.

Файл .env читает docker compose (env_file) и пробрасывает в контейнер;
сам Python никаких файлов не разбирает, поэтому в тестах достаточно
подменить переменную окружения и сбросить кэш get_settings().
"""

from functools import lru_cache
from typing import Annotated, Any

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEFAULT_CORS_ORIGINS = ["http://localhost:5173"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore")

    # postgresql+asyncpg://user:password@host:5432/dbname
    database_url: str

    # Если не задан, тесты выводят адрес сами: имя базы из database_url + "_test".
    test_database_url: str | None = None

    # NoDecode: без него pydantic-settings попытается разобрать значение как JSON.
    # Нам нужен обычный список через запятую: "http://a,http://b".
    cors_origins: Annotated[list[str], NoDecode] = DEFAULT_CORS_ORIGINS

    db_echo: bool = False

    # Секрет подписи токенов. Дефолта нет намеренно: пустой секрет должен
    # ронять старт, а не молча подписывать токены предсказуемым ключом.
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 1440

    # Инвариант 3: бронь в held живёт 10 минут. Срок здесь, а не в схеме БД:
    # это бизнес-правило, в схеме его смена требовала бы миграции.
    reservation_ttl_seconds: int = 600

    # Свипер истёкших броней (ADR-010, ADR-019).
    # Выключается только явным флагом: случайный ноль в интервале не должен
    # молча останавливать возврат броней на витрину.
    sweeper_enabled: bool = True
    # gt=0: нулевой или отрицательный интервал — это не «выключить», а крутить
    # цикл без паузы, поэтому такая настройка роняет старт приложения.
    sweeper_interval_seconds: int = Field(default=2, gt=0)

    # --- Платёжная заглушка (ADR-005, ADR-021) ---
    # Адрес внутри docker-сети: заглушка — отдельный сервис.
    payment_stub_url: str = "http://payment-stub:8000"
    # База для callback_url, который мы передаём заглушке. Тоже внутренний
    # адрес: вебхук идёт от сервиса к сервису, а не через браузер.
    payment_callback_base_url: str = "http://backend:8000"
    # Общий секрет подписи вебхуков — тот же, что у заглушки.
    payment_webhook_secret: str = "dev-only-webhook-secret"
    payment_request_timeout_seconds: float = Field(default=5.0, gt=0)

    # Сверка зависших платежей (ADR-023). Выключается только явным флагом:
    # случайный ноль в интервале не должен молча останавливать досчёт заказов.
    reconciler_enabled: bool = True
    reconcile_interval_seconds: int = Field(default=5, gt=0)
    # Льготный период: свежим платежам даём шанс получить вебхук, не дёргая
    # провайдера зря.
    reconcile_grace_seconds: int = Field(default=10, ge=0)
    # Нарастающий интервал между проверками одного платежа: base * 2^попытки,
    # но не больше max. Потолок — это и обещание клиенту: при потерянном
    # вебхуке заказ досчитается не позже чем через reconcile_backoff_max.
    reconcile_backoff_base_seconds: int = Field(default=5, gt=0)
    reconcile_backoff_max_seconds: int = Field(default=300, gt=0)
    reconcile_batch_size: int = Field(default=50, gt=0)

    # --- Почта и отправка outbox (ADR-004, ADR-024) ---
    smtp_host: str = "mailpit"
    smtp_port: int = Field(default=1025, gt=0)
    # Таймаут обязателен и проверяется на старте: он должен быть заметно
    # меньше аренды, иначе медленный SMTP даст дубль в штатной работе —
    # аренда истечёт, пока первый отправитель ещё ждёт ответа.
    smtp_timeout_seconds: float = Field(default=10.0, gt=0)
    mail_from: str = "no-reply@flash-sale.local"
    mail_from_name: str = "Flash Sale"

    outbox_enabled: bool = True
    outbox_interval_seconds: int = Field(default=2, gt=0)
    outbox_batch_size: int = Field(default=20, gt=0)
    outbox_max_attempts: int = Field(default=5, ge=1)
    outbox_retry_base_seconds: int = Field(default=5, gt=0)
    # Аренда строки на время отправки.
    outbox_lease_seconds: int = Field(default=30, gt=0)

    # Закрытие распродаж по времени (ADR-025). Выключается только явным флагом.
    sale_closer_enabled: bool = True
    sale_closer_interval_seconds: int = Field(default=5, gt=0)

    @model_validator(mode="after")
    def _smtp_timeout_fits_lease(self) -> "Settings":
        """Таймаут SMTP должен быть заметно меньше аренды.

        Иначе первый отправитель ещё ждёт SMTP, аренда уже истекла, второй
        подхватывает ту же строку — и письмо уходит дважды без всякого
        падения процесса. Требуем двукратный запас.
        """
        if self.smtp_timeout_seconds * 2 > self.outbox_lease_seconds:
            raise ValueError(
                "SMTP_TIMEOUT_SECONDS должен быть не больше половины "
                f"OUTBOX_LEASE_SECONDS (сейчас {self.smtp_timeout_seconds} "
                f"против {self.outbox_lease_seconds}): иначе аренда истечёт, "
                "пока отправитель ещё ждёт SMTP, и письмо уйдёт дважды"
            )
        return self

    # Окно склейки live-обновлений (ADR-020). Сигналы, пришедшие за это время,
    # обрабатываются одной пачкой: пачка броней даёт один запрос к БД и одну
    # рассылку. Осязаемый компромисс задержка/нагрузка, поэтому настройка.
    live_update_window_ms: int = Field(default=50, ge=0)

    @property
    def asyncpg_dsn(self) -> str:
        """DSN для прямого соединения asyncpg.

        Слушатель LISTEN живёт вне пула SQLAlchemy, поэтому ему нужен адрес
        без драйверного префикса.
        """
        return self.database_url.replace("+asyncpg", "", 1)

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_cors_origins(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # значения приходят из окружения
