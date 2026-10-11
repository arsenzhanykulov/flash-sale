"""Настройки приложения. Источник — только переменные окружения.

Файл .env читает docker compose (env_file) и пробрасывает в контейнер;
сам Python никаких файлов не разбирает, поэтому в тестах достаточно
подменить переменную окружения и сбросить кэш get_settings().
"""

from functools import lru_cache
from typing import Annotated, Any

from pydantic import Field, field_validator
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
