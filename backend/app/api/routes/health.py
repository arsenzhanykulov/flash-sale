"""GET /health — живость сервиса вместе с проверкой соединения с БД."""

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from app.api.schemas.health import HealthResponse
from app.services.health import ping_db

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": HealthResponse}},
    summary="Проверка живости сервиса и соединения с БД",
)
async def health() -> HealthResponse | JSONResponse:
    if not await ping_db():
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=HealthResponse(status="error", database="unavailable").model_dump(),
        )
    return HealthResponse(status="ok", database="ok")
