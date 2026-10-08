"""Вход по email без пароля (ADR-007)."""

from fastapi import APIRouter, status

from app.api.deps import CurrentUser, SessionDep
from app.api.schemas.auth import LoginRequest, LoginResponse, UserResponse
from app.core.security import create_access_token
from app.services.auth import login_or_create_buyer

router = APIRouter()


@router.post(
    "/login",
    response_model=LoginResponse,
    summary="Вход по email: отдаёт токен, неизвестного создаёт покупателем",
)
async def login(payload: LoginRequest, session: SessionDep) -> LoginResponse:
    user = await login_or_create_buyer(session, payload.email)
    return LoginResponse(
        access_token=create_access_token(user.id),
        user=UserResponse.model_validate(user),
    )


@router.get(
    "/me",
    response_model=UserResponse,
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Токен отсутствует или недействителен"}
    },
    summary="Текущий пользователь по токену",
)
async def me(current_user: CurrentUser) -> UserResponse:
    return UserResponse.model_validate(current_user)
