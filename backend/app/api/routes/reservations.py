"""Резерв единицы товара и отмена брони."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser, SessionDep
from app.api.schemas.reservations import (
    CancelErrorResponse,
    MyReservationsResponse,
    ReservationResponse,
    ReserveErrorResponse,
)
from app.services.reservations import (
    CancelError,
    CancelRefusal,
    ReserveError,
    ReserveRefusal,
    cancel_reservation,
    create_reservation,
    list_my_active_reservations,
)

# Вложенный в распродажу: POST /api/sales/{sale_id}/reservations.
sale_scoped_router = APIRouter()

# Операции над своими бронями: /api/reservations.
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

CANCEL_REFUSALS: dict[CancelRefusal, tuple[int, str]] = {
    CancelRefusal.RESERVATION_NOT_FOUND: (status.HTTP_404_NOT_FOUND, "Бронь не найдена"),
    CancelRefusal.NOT_CANCELLABLE: (
        status.HTTP_409_CONFLICT,
        "Бронь нельзя отменить: оплата уже начата",
    ),
}


@sale_scoped_router.post(
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


# Объявлено выше /{reservation_id}, чтобы «me» не читалось как идентификатор.
@router.get(
    "/me",
    response_model=MyReservationsResponse,
    responses={status.HTTP_401_UNAUTHORIZED: {"description": "Требуется вход"}},
    summary="Мои активные брони (held и paying)",
)
async def my_reservations(
    current_user: CurrentUser,
    session: SessionDep,
) -> MyReservationsResponse:
    view = await list_my_active_reservations(session, user_id=current_user.id)
    return MyReservationsResponse.model_validate(view)


@router.delete(
    "/{reservation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        status.HTTP_204_NO_CONTENT: {
            "description": "Бронь отменена, либо уже была завершена — тела нет"
        },
        status.HTTP_401_UNAUTHORIZED: {"description": "Требуется вход"},
        status.HTTP_404_NOT_FOUND: {
            "model": CancelErrorResponse,
            "description": "Брони нет или она принадлежит другому покупателю",
        },
        status.HTTP_409_CONFLICT: {
            "model": CancelErrorResponse,
            "description": "code: not_cancellable — оплата уже начата",
        },
    },
    summary="Отменить свою бронь",
)
async def cancel_reservation_endpoint(
    reservation_id: UUID,
    current_user: CurrentUser,
    session: SessionDep,
) -> None:
    """Идемпотентно: повторная отмена и уже завершённая бронь тоже дают 204."""
    try:
        await cancel_reservation(
            session,
            reservation_id=reservation_id,
            user_id=current_user.id,
        )
    except CancelError as error:
        http_status, message = CANCEL_REFUSALS[error.reason]
        raise HTTPException(
            status_code=http_status,
            detail={"code": error.reason.value, "message": message},
        ) from error
