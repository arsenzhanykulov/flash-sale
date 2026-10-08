"""Упрощённый вход по email (ADR-007)."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Один запрос и на создание, и на получение существующего пользователя:
# без «проверили SELECT-ом → вставили», которое проигрывает гонку при
# двух одновременных входах с одного email.
#
# DO UPDATE, а не DO NOTHING — иначе RETURNING не отдаёт строку при повторном
# входе. Обновляется только email: роль существующего пользователя не трогаем,
# иначе вход магазина сделал бы его покупателем.
LOGIN_SQL = text("""
    insert into users (email, role)
    values (:email, 'buyer')
    on conflict (email) do update set email = excluded.email
    returning id, email, role
""")

GET_USER_SQL = text("select id, email, role from users where id = :user_id")


@dataclass(frozen=True)
class AuthenticatedUser:
    id: UUID
    email: str
    role: str


def normalize_email(email: str) -> str:
    """Email хранится в lower-case (CHECK в схеме этого же требует)."""
    return email.strip().lower()


async def login_or_create_buyer(session: AsyncSession, email: str) -> AuthenticatedUser:
    row = (await session.execute(LOGIN_SQL, {"email": normalize_email(email)})).one()
    await session.commit()
    return AuthenticatedUser(id=row.id, email=row.email, role=row.role)


async def get_user_by_id(session: AsyncSession, user_id: UUID) -> AuthenticatedUser | None:
    row = (await session.execute(GET_USER_SQL, {"user_id": user_id})).one_or_none()
    if row is None:
        return None
    return AuthenticatedUser(id=row.id, email=row.email, role=row.role)
