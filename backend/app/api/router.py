"""Сборка роутеров API: здесь и только здесь задаются префиксы путей."""

from fastapi import APIRouter

from app.api.routes import auth, health, sales, time

api_router = APIRouter()

# /health — инфраструктурный эндпоинт, живёт в корне.
api_router.include_router(health.router)

api_router.include_router(time.router, prefix="/api", tags=["time"])
api_router.include_router(auth.router, prefix="/api/auth", tags=["auth"])
api_router.include_router(sales.router, prefix="/api/sales", tags=["sales"])
