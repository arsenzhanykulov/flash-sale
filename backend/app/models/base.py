"""Общая база для моделей SQLAlchemy.

Схема задаётся строго по docs/DATA_MODEL.md: id — UUID, время — timestamptz (UTC),
деньги — BIGINT в минимальных единицах, статусы — text с CHECK (без нативных ENUM).
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from sqlalchemy import CHAR, BigInteger, DateTime, Integer, MetaData, Text, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, mapped_column

# Явные имена ограничений и индексов. Без этого Postgres придумывает их сам,
# имена различаются между окружениями, и downgrade не находит, что удалять.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def status_in(column: str, statuses: type[StrEnum]) -> str:
    """SQL-условие `column IN (...)` из StrEnum.

    CHECK собирается из того же перечисления, что использует код, — чтобы
    допустимые статусы в схеме и в приложении не разъехались.
    """
    values = ", ".join(f"'{status.value}'" for status in statuses)
    return f"{column} IN ({values})"


# Id генерирует Postgres: явный SQL критичных операций вставляет строки,
# не заботясь о генерации id в Python. gen_random_uuid() в PG 13+ встроена.
uuid_pk = Annotated[
    UUID,
    mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    ),
]
uuid_fk = Annotated[UUID, mapped_column(PgUUID(as_uuid=True))]

# Инвариант 9: время в БД — только timestamptz (UTC).
timestamptz = Annotated[datetime, mapped_column(DateTime(timezone=True))]
timestamptz_null = Annotated[datetime | None, mapped_column(DateTime(timezone=True))]
created_at = Annotated[
    datetime,
    mapped_column(DateTime(timezone=True), server_default=text("now()")),
]

# Инвариант 8: деньги — целые числа в минимальных единицах, никаких float.
money_minor = Annotated[int, mapped_column(BigInteger)]
currency_code = Annotated[str, mapped_column(CHAR(3))]

counter = Annotated[int, mapped_column(Integer, server_default=text("0"))]
required_text = Annotated[str, mapped_column(Text)]
optional_text = Annotated[str | None, mapped_column(Text)]
