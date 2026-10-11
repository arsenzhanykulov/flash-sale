"""API заглушки."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.config import PaymentMode
from app.processing import PaymentProcessor
from app.storage import AmountMismatch, ModeSettings, Payment, PaymentStatus, Storage

router = APIRouter()


def get_storage(request: Request) -> Storage:
    return request.app.state.storage


def get_processor(request: Request) -> PaymentProcessor:
    return request.app.state.processor


StorageDep = Annotated[Storage, Depends(get_storage)]
ProcessorDep = Annotated[PaymentProcessor, Depends(get_processor)]


class CreatePaymentRequest(BaseModel):
    idempotency_key: str = Field(min_length=1)
    amount_minor: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    callback_url: str = Field(min_length=1)


class PaymentResponse(BaseModel):
    payment_id: str
    # Ключ виден в ответе намеренно: по нему удобно сверять идемпотентность
    # на страничке управления и в отладке.
    idempotency_key: str
    status: PaymentStatus
    amount_minor: int
    currency: str
    mode: PaymentMode
    webhook_attempts: int
    webhooks_sent: int


def to_response(payment: Payment) -> PaymentResponse:
    return PaymentResponse(
        payment_id=payment.id,
        idempotency_key=payment.idempotency_key,
        status=payment.status,
        amount_minor=payment.amount_minor,
        currency=payment.currency,
        mode=payment.mode,
        webhook_attempts=payment.webhook_attempts,
        webhooks_sent=payment.webhooks_sent,
    )


class ModeRequest(BaseModel):
    mode: PaymentMode
    delay_ms: int | None = Field(default=None, ge=0)
    duplicate_webhooks: bool | None = None


class ModeResponse(BaseModel):
    mode: PaymentMode
    delay_ms: int
    duplicate_webhooks: bool


class ResolveRequest(BaseModel):
    result: PaymentMode = Field(description="approve или decline")


RESOLVE_OUTCOMES: dict[PaymentMode, PaymentStatus] = {
    PaymentMode.APPROVE: PaymentStatus.SUCCEEDED,
    PaymentMode.DECLINE: PaymentStatus.DECLINED,
}


@router.post(
    "/payments",
    response_model=PaymentResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        status.HTTP_200_OK: {"description": "Платёж с таким ключом уже существует"},
        status.HTTP_409_CONFLICT: {"description": "Тот же ключ с другой суммой или валютой"},
    },
    summary="Создать платёж (идемпотентно по ключу)",
)
async def create_payment(
    payload: CreatePaymentRequest,
    storage: StorageDep,
    processor: ProcessorDep,
    response: Response,
) -> PaymentResponse:
    mode = await storage.get_mode()
    try:
        payment, created = await storage.create_or_get(
            idempotency_key=payload.idempotency_key,
            amount_minor=payload.amount_minor,
            currency=payload.currency.upper(),
            callback_url=payload.callback_url,
            mode=mode,
        )
    except AmountMismatch:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "amount_mismatch",
                "message": "Ключ идемпотентности уже использован с другой суммой",
            },
        ) from None

    if created:
        # Задержку отрабатывает фоновая задача: ответ 202 уходит сразу.
        processor.schedule(payment)
    else:
        # Повторный вызов с тем же ключом — платёж не создан заново.
        response.status_code = status.HTTP_200_OK

    return to_response(payment)


@router.get(
    "/payments/{payment_id}",
    response_model=PaymentResponse,
    responses={status.HTTP_404_NOT_FOUND: {"description": "Платёж не найден"}},
    summary="Текущий статус платежа",
)
async def get_payment(payment_id: str, storage: StorageDep) -> PaymentResponse:
    payment = await storage.get(payment_id)
    if payment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Платёж не найден")
    return to_response(payment)


@router.get("/payments", response_model=list[PaymentResponse], summary="Список платежей")
async def list_payments(storage: StorageDep) -> list[PaymentResponse]:
    return [to_response(payment) for payment in await storage.list_payments()]


@router.post(
    "/payments/{payment_id}/resolve",
    response_model=PaymentResponse,
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Платёж не найден"},
        status.HTTP_409_CONFLICT: {"description": "Платёж уже в окончательном статусе"},
    },
    summary="Отпустить зависший платёж вручную",
)
async def resolve_payment(
    payment_id: str,
    payload: ResolveRequest,
    storage: StorageDep,
    processor: ProcessorDep,
) -> PaymentResponse:
    payment = await storage.get(payment_id)
    if payment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Платёж не найден")

    settled = await processor.settle(payment_id, RESOLVE_OUTCOMES[payload.result])
    if settled is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "already_resolved",
                "message": f"Платёж уже в статусе {payment.status.value}",
            },
        )
    return to_response(settled)


@router.get("/control/mode", response_model=ModeResponse, summary="Текущий режим")
async def read_mode(storage: StorageDep) -> ModeResponse:
    mode = await storage.get_mode()
    return ModeResponse(
        mode=mode.mode, delay_ms=mode.delay_ms, duplicate_webhooks=mode.duplicate_webhooks
    )


@router.post("/control/mode", response_model=ModeResponse, summary="Переключить режим на лету")
async def set_mode(payload: ModeRequest, storage: StorageDep) -> ModeResponse:
    current = await storage.get_mode()
    # Незаданные поля оставляем как были: переключать режим можно,
    # не повторяя каждый раз задержку.
    updated = ModeSettings(
        mode=payload.mode,
        delay_ms=current.delay_ms if payload.delay_ms is None else payload.delay_ms,
        duplicate_webhooks=(
            current.duplicate_webhooks
            if payload.duplicate_webhooks is None
            else payload.duplicate_webhooks
        ),
    )
    await storage.set_mode(updated)
    return ModeResponse(
        mode=updated.mode,
        delay_ms=updated.delay_ms,
        duplicate_webhooks=updated.duplicate_webhooks,
    )


@router.get("/health", summary="Живость сервиса")
async def health(storage: StorageDep) -> dict[str, str]:
    await storage.get_mode()
    return {"status": "ok"}
