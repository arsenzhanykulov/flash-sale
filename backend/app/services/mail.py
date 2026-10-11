"""Сборка и отправка писем.

Содержимое собирается **только из payload строки outbox** — отправитель
не ходит в другие таблицы (ADR-024). Так письмо остаётся тем, каким его
задумали в момент события, даже если данные потом изменились.

Шаблон выбирается по `kind`: на следующих шагах добавится письмо о закрытии
распродажи, и для него достаточно будет дописать одну функцию в реестр.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr

import aiosmtplib

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Домен в Message-ID: он должен быть постоянным, чтобы повторная отправка
# того же письма имела тот же идентификатор и могла быть опознана как дубль.
MESSAGE_ID_DOMAIN = "flash-sale.local"


class UnknownMailKind(Exception):
    """Для такого `kind` нет шаблона — отправлять нечего."""


class MailDeliveryError(Exception):
    """SMTP не принял письмо."""


@dataclass(frozen=True)
class MailContent:
    subject: str
    body: str


def format_money(amount_minor: int, currency: str) -> str:
    """Деньги — целые в минимальных единицах (инвариант 8), без float."""
    whole, fraction = divmod(amount_minor, 100)
    return f"{whole},{fraction:02d} {currency}"


def order_paid(payload: dict) -> MailContent:
    product = payload["product_name"]
    amount = format_money(payload["amount_minor"], payload["currency"])
    order_id = payload["order_id"]
    return MailContent(
        subject=f"Заказ оплачен — {product}",
        body=(
            "Здравствуйте!\n\n"
            f"Ваш заказ на «{product}» оплачен.\n\n"
            f"Сумма: {amount}\n"
            f"Номер заказа: {order_id}\n\n"
            "Товар закреплён за вами — магазин свяжется с вами о доставке.\n\n"
            "Спасибо за покупку!\n"
        ),
    )


# Реестр шаблонов по виду письма. Новый вид — новая функция и строка здесь.
TEMPLATES: dict[str, Callable[[dict], MailContent]] = {
    "order_paid": order_paid,
}


def render(kind: str, payload: dict) -> MailContent:
    template = TEMPLATES.get(kind)
    if template is None:
        raise UnknownMailKind(f"нет шаблона для kind={kind!r}")
    try:
        return template(payload)
    except KeyError as error:
        raise UnknownMailKind(f"в payload письма {kind!r} нет поля {error}") from error


def message_id(dedup_key: str) -> str:
    """Постоянный Message-ID из dedup_key.

    Повторная отправка того же письма получит тот же идентификатор, поэтому
    дубль можно опознать и подавить на стороне получателя (ADR-024).
    """
    return f"<{dedup_key.replace(':', '.')}@{MESSAGE_ID_DOMAIN}>"


def build_message(*, kind: str, dedup_key: str, recipient: str, payload: dict) -> EmailMessage:
    content = render(kind, payload)
    settings = get_settings()

    message = EmailMessage()
    message["From"] = formataddr((settings.mail_from_name, settings.mail_from))
    message["To"] = recipient
    message["Subject"] = content.subject
    message["Message-ID"] = message_id(dedup_key)
    message.set_content(content.body)
    return message


class SmtpMailer:
    """Отправка через SMTP. Таймаут обязателен — см. ADR-024."""

    async def send(self, message: EmailMessage) -> None:
        settings = get_settings()
        try:
            await aiosmtplib.send(
                message,
                hostname=settings.smtp_host,
                port=settings.smtp_port,
                timeout=settings.smtp_timeout_seconds,
            )
        except Exception as error:
            raise MailDeliveryError(str(error)) from error
