"""Настройки приложения. Источник — только переменные окружения.

Файл .env читает docker compose (env_file) и пробрасывает в контейнер;
сам Python никаких файлов не разбирает, поэтому в тестах достаточно
подменить переменную окружения и сбросить кэш get_settings().
"""

from functools import lru_cache
from typing import Annotated, Any

from pydantic import field_validator
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

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_cors_origins(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # значения приходят из окружения
