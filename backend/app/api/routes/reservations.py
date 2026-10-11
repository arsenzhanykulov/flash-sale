"""Резерв единицы товара и отмена брони."""

import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException, Response, status

from app.api.deps import CurrentUser, SessionDep
from app.api.schemas.payments import PayErrorResponse, PayResponse
from app.api.schemas.reservations import (
    CancelErrorResponse,
    MyReservationsResponse,
    ReservationResponse,
    ReserveErrorResponse,
)
from app.services.payments import (
    PayError,
    PayRefusal,
    ProviderState,
    ProviderUnavailable,
    callback_url,
    get_payment_provider,
    start_payment,
)
from app.services.payments.flow import record_provider_payment_id
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

logger = logging.getLogger(__name__)

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


PAY_REFUSALS: dict[PayRefusal, tuple[int, str]] = {
    PayRefusal.RESERVATION_NOT_FOUND: (status.HTTP_404_NOT_FOUND, "Бронь не найдена"),
    PayRefusal.RESERVATION_EXPIRED: (
        status.HTTP_409_CONFLICT,
        "Бронь истекла — единица вернулась на витрину",
    ),
    PayRefusal.RESERVATION_CANCELLED: (status.HTTP_409_CONFLICT, "Бронь отменена"),
    PayRefusal.SALE_ENDED: (
        status.HTTP_409_CONFLICT,
        "Распродажа завершена — оплату начать уже нельзя",
    ),
    PayRefusal.PAYMENT_FAILED: (
        status.HTTP_409_CONFLICT,
        "Предыдущая оплата была отклонена, бронь больше не действует",
    ),
    PayRefusal.SALE_NOT_PAYABLE: (status.HTTP_409_CONFLICT, "Оплата сейчас невозможна"),
}


@router.post(
    "/{reservation_id}/pay",
    response_model=PayResponse,
    responses={
        status.HTTP_202_ACCEPTED: {
            "model": PayResponse,
            "description": "Заказ создан, но провайдер не ответил — ждём сверки",
        },
        status.HTTP_401_UNAUTHORIZED: {"description": "Требуется вход"},
        status.HTTP_404_NOT_FOUND: {
            "model": PayErrorResponse,
            "description": "Брони нет или она принадлежит другому покупателю",
        },
        status.HTTP_409_CONFLICT: {
            "model": PayErrorResponse,
            "description": "code: reservation_expired, reservation_cancelled, payment_failed",
        },
    },
    summary="Оплатить свою бронь",
)
async def pay_reservation(
    reservation_id: UUID,
    current_user: CurrentUser,
    session: SessionDep,
    response: Response,
) -> PayResponse:
    """Перевести бронь в paying, создать заказ и начать платёж.

    Вызов провайдера идёт ПОСЛЕ коммита: сетевой запрос не держит блокировки
    БД, а бронь в paying не истекает (инвариант 4).
    """
    try:
        result, idempotency_key, amount_minor, currency = await start_payment(
            session, reservation_id=reservation_id, user_id=current_user.id
        )
    except PayError as error:
        http_status, message = PAY_REFUSALS[error.reason]
        raise HTTPException(
            status_code=http_status,
            detail={"code": error.reason.value, "message": message},
        ) from error

    provider_state = ProviderState.ACCEPTED
    try:
        provider_payment = await get_payment_provider().create_payment(
            idempotency_key=idempotency_key,
            amount_minor=amount_minor,
            currency=currency,
            callback_url=callback_url(),
        )
    except ProviderUnavailable as error:
        # Не 500: заказ создан, бронь в paying, догонит сверка.
        logger.warning("платёжная система недоступна для заказа %s: %s", result.order_id, error)
        provider_state = ProviderState.AWAITING_PROVIDER
        response.status_code = status.HTTP_202_ACCEPTED
    else:
        await record_provider_payment_id(
            session, result.payment_id, provider_payment.provider_payment_id
        )

    return PayResponse(
        order_id=result.order_id,
        payment_id=result.payment_id,
        order_status=result.order_status,
        payment_status=result.payment_status,
        amount_minor=result.amount_minor,
        currency=result.currency,
        provider_state=provider_state,
        already_started=result.already_started,
    )
