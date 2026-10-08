"""Схемы запросов и ответов API.

По модулю на домен — так же, как устроены роутеры в `app/api` и модели
в `app/models`. Роутеры только собирают ответ из этих схем, валидация
и форма контракта живут здесь.
"""

from app.api.schemas.auth import LoginRequest, LoginResponse, UserResponse
from app.api.schemas.health import HealthResponse
from app.api.schemas.sales import SaleResponse
from app.api.schemas.time import ServerTimeResponse

__all__ = [
    "HealthResponse",
    "LoginRequest",
    "LoginResponse",
    "SaleResponse",
    "ServerTimeResponse",
    "UserResponse",
]
