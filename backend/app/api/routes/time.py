"""Серверное время для синхронизации таймеров на фронте."""

from fastapi import APIRouter

from app.api.deps import SessionDep
from app.api.schemas.time import ServerTimeResponse
from app.services.server_time import get_server_time

router = APIRouter()


@router.get(
    "/time",
    response_model=ServerTimeResponse,
    summary="Текущее время сервера (now() в БД)",
)
async def server_time(session: SessionDep) -> ServerTimeResponse:
    return ServerTimeResponse(now=await get_server_time(session))
