"""Резерв единицы товара.

Шаг 2.0: под эндпоинтом лежит наивная реализация с гонкой — она помечена
предупреждением в `app/services/reservations.py` и будет заменена на 2.1.
"""

from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser, SessionDep
from app.api.schemas.reservations import ReservationResponse
from app.services.reservations import (
    ReserveError,
    ReserveRefusal,
    create_reservation_naive,
)

router = APIRouter()

# Отказ → код ответа и текст. Распродажа не найдена — 404, всё остальное
# это конфликт с текущим состоянием распродажи, то есть 409.
REFUSALS: dict[ReserveRefusal, tuple[int, str]] = {
    ReserveRefusal.SALE_NOT_FOUND: (status.HTTP_404_NOT_FOUND, "Распродажа не найдена"),
    ReserveRefusal.NOT_STARTED: (status.HTTP_409_CONFLICT, "Распродажа ещё не началась"),
    ReserveRefusal.ENDED: (status.HTTP_409_CONFLICT, "Распродажа завершена"),
    ReserveRefusal.SOLD_OUT: (status.HTTP_409_CONFLICT, "Товар разобрали"),
    ReserveRefusal.ALREADY_RESERVED: (
        status.HTTP_409_CONFLICT,
        "У вас уже есть активная бронь на эту распродажу",
    ),
}


@router.post(
    "/{sale_id}/reservations",
    response_model=ReservationResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Требуется вход"},
        status.HTTP_404_NOT_FOUND: {"description": "Распродажа не найдена"},
        status.HTTP_409_CONFLICT: {
            "description": "Распродажа не идёт, товар разобрали или бронь уже есть"
        },
    },
    summary="Забронировать одну единицу товара",
)
async def create_reservation(
    sale_id: UUID,
    current_user: CurrentUser,
    session: SessionDep,
) -> ReservationResponse:
    try:
        reservation = await create_reservation_naive(
            session,
            sale_id=sale_id,
            user_id=current_user.id,
        )
    except ReserveError as error:
        code, detail = REFUSALS[error.reason]
        raise HTTPException(status_code=code, detail=detail) from error

    return ReservationResponse.model_validate(reservation)
