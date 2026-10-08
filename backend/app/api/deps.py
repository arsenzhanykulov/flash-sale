"""Зависимости FastAPI: сессия БД и текущий пользователь из токена."""

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import InvalidToken, decode_access_token
from app.services.auth import AuthenticatedUser, get_user_by_id

# auto_error=False: со включённым автоответом отсутствие заголовка даёт 403,
# а для «не аутентифицирован» правильный код — 401.
bearer_scheme = HTTPBearer(auto_error=False, description="Токен из POST /api/auth/login")

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> AuthenticatedUser:
    if credentials is None:
        raise unauthorized("Требуется токен доступа")

    try:
        user_id = decode_access_token(credentials.credentials)
    except InvalidToken as error:
        raise unauthorized("Недействительный токен доступа") from error

    user = await get_user_by_id(session, user_id)
    if user is None:
        # Токен подписан нами, но пользователя больше нет.
        raise unauthorized("Пользователь не найден")
    return user


CurrentUser = Annotated[AuthenticatedUser, Depends(get_current_user)]
