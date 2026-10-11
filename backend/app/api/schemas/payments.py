from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.services.payments import PayRefusal, ProviderState


class PayResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    order_id: UUID
    payment_id: UUID
    order_status: str
    payment_status: str
    amount_minor: int
    currency: str
    # accepted — провайдер принял платёж; awaiting_provider — он не ответил,
    # заказ создан и ждёт сверки.
    provider_state: ProviderState
    # True, если оплата была начата раньше: повторный клик не создал второй заказ.
    already_started: bool


class PayErrorDetail(BaseModel):
    code: PayRefusal
    message: str


class PayErrorResponse(BaseModel):
    detail: PayErrorDetail


class WebhookPayload(BaseModel):
    payment_id: str
    idempotency_key: str
    status: str


class WebhookAck(BaseModel):
    # applied=False означает «уже обработан»: провайдеру всё равно 200,
    # чтобы он перестал повторять.
    applied: bool
