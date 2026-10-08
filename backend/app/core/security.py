"""Токены доступа.

ADR-007: вход по email без пароля, состояние сессии не храним — подписанный
токен сам себя подтверждает. Срок жизни и секрет берутся из настроек.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
from jwt import InvalidTokenError

from app.core.config import get_settings


class InvalidToken(Exception):
    """Токен повреждён, просрочен или подписан не нашим секретом."""


def create_access_token(user_id: UUID) -> str:
    settings = get_settings()
    issued_at = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "iat": issued_at,
        "exp": issued_at + timedelta(minutes=settings.access_token_ttl_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> UUID:
    """Вернуть id пользователя из токена или бросить InvalidToken.

    Срок действия проверяет сама библиотека; `require` не даёт принять
    токен без `exp` — такой жил бы вечно.
    """
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["sub", "exp"]},
        )
        return UUID(payload["sub"])
    except (InvalidTokenError, ValueError) as error:
        raise InvalidToken(str(error)) from error
