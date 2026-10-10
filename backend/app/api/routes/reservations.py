"""Резерв единицы товара."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser, SessionDep
from app.api.schemas.reservations import ReservationResponse, ReserveErrorResponse
from app.services.reservations import ReserveError, ReserveRefusal, create_reservation

router = APIRouter()

# Распродажа не найдена — 404. Остальные отказы это конфликт с текущим
# состоянием распродажи, то есть 409: 425 Too Early предназначен для TLS
# early-data, а 410 Gone говорит о безвозвратно удалённом ресурсе —
# распродажа же существует и читается.
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
        status.HTTP_404_NOT_FOUND: {
            "model": ReserveErrorResponse,
            "description": "Распродажа не найдена (code: sale_not_found)",
        },
        status.HTTP_409_CONFLICT: {
            "model": ReserveErrorResponse,
            "description": "code: not_started, ended, sold_out или already_reserved",
        },
    },
    summary="Забронировать одну единицу товара",
)
async def create_reservation_endpoint(
    sale_id: UUID,
    current_user: CurrentUser,
    session: SessionDep,
) -> ReservationResponse:
    try:
        reservation = await create_reservation(
            session,
            sale_id=sale_id,
            user_id=current_user.id,
        )
    except ReserveError as error:
        http_status, message = REFUSALS[error.reason]
        raise HTTPException(
            status_code=http_status,
            detail={"code": error.reason.value, "message": message},
        ) from error

    return ReservationResponse.model_validate(reservation)
