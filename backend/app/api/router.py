"""Сборка роутеров API: здесь и только здесь задаются префиксы путей."""

from fastapi import APIRouter

from app.api.routes import auth, health, reservations, sales, time

api_router = APIRouter()

# /health — инфраструктурный эндпоинт, живёт в корне.
api_router.include_router(health.router)

api_router.include_router(time.router, prefix="/api", tags=["time"])
api_router.include_router(auth.router, prefix="/api/auth", tags=["auth"])
api_router.include_router(sales.router, prefix="/api/sales", tags=["sales"])
# Создание брони вложено в распродажу: POST /api/sales/{sale_id}/reservations.
api_router.include_router(
    reservations.sale_scoped_router, prefix="/api/sales", tags=["reservations"]
)
# Операции над своими бронями: GET /api/reservations/me, DELETE /api/reservations/{id}.
api_router.include_router(reservations.router, prefix="/api/reservations", tags=["reservations"])
