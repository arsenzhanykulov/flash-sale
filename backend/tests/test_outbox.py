"""Отправка писем из outbox.

Основная часть идёт на фейковом отправителе. Отдельный тест в конце проверяет
доставку в **настоящий Mailpit** через его API — он в compose есть всегда,
поэтому пропускается только при реально недоступном сервисе, с явной причиной.
"""

import asyncio
import json
from dataclasses import dataclass, field
from email.message import EmailMessage
from uuid import UUID, uuid4

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.services.mail import MailDeliveryError, build_message, message_id
from app.services.outbox import send_outbox_once
from app.services.payments import reconcile_once
from app.services.payments.flow import idempotency_key_for, order_paid_dedup_key
from tests.conftest import (
    FakePaymentProvider,
    MakeSale,
    assert_counters_consistent,
    run_with_barrier,
    webhook_headers,
)

MAILPIT_API = "http://mailpit:8025"


@dataclass
class FakeMailer:
    """Фейковый отправитель: считает письма и умеет падать."""

    fail_times: int = 0
    sent: list[EmailMessage] = field(default_factory=list)
    # Задержка перед «отправкой» — чтобы проверить гонку аренды.
    delay_seconds: float = 0.0

    async def send(self, message: EmailMessage) -> None:
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise MailDeliveryError("тестовый отказ SMTP")
        self.sent.append(message)

    def subjects(self) -> list[str]:
        return [message["Subject"] for message in self.sent]

    def message_ids(self) -> list[str]:
        return [message["Message-ID"] for message in self.sent]


@pytest.fixture
def mailer() -> FakeMailer:
    return FakeMailer()


async def login(client: AsyncClient) -> str:
    response = await client.post(
        "/api/auth/login", json={"email": f"mail-{uuid4().hex[:12]}@example.com"}
    )
    return response.json()["access_token"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def pay_and_settle(
    client: AsyncClient,
    sale_id: UUID,
    token: str,
    *,
    webhooks: int = 1,
) -> tuple[str, str]:
    """Полный путь до оплаченного заказа. Возвращает (order_id, dedup_key)."""
    reservation = (
        await client.post(f"/api/sales/{sale_id}/reservations", headers=auth(token))
    ).json()
    order = (
        await client.post(f"/api/reservations/{reservation['id']}/pay", headers=auth(token))
    ).json()
    key = idempotency_key_for(UUID(reservation["id"]))

    body = json.dumps(
        {"payment_id": "stub-mail", "idempotency_key": key, "status": "succeeded"},
        separators=(",", ":"),
    ).encode()
    for _ in range(webhooks):
        response = await client.post(
            "/api/payments/webhook", content=body, headers=webhook_headers(body)
        )
        assert response.status_code == 200, response.text

    return order["order_id"], order_paid_dedup_key(UUID(order["order_id"]))


async def letter_row(session: AsyncSession, dedup_key: str) -> dict:
    await session.rollback()
    row = (
        await session.execute(
            text("""
                select status, attempts, sent_at, last_error,
                       next_attempt_at > now() as retry_in_future,
                       locked_until
                from outbox where dedup_key = :key
            """),
            {"key": dedup_key},
        )
    ).one()
    return dict(row._mapping)


async def letters_with_key(session: AsyncSession, dedup_key: str) -> int:
    await session.rollback()
    return await session.scalar(
        text("select count(*) from outbox where dedup_key = :key"), {"key": dedup_key}
    )


async def test_one_payment_sends_one_letter(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
    mailer: FakeMailer,
) -> None:
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    order_id, dedup_key = await pay_and_settle(client, sale_id, token)

    report = await send_outbox_once(session, mailer)

    assert report.sent >= 1
    assert len(mailer.sent) == 1
    message = mailer.sent[0]
    assert message["Subject"].startswith("Заказ оплачен —")
    assert order_id in message.get_content()
    # Сумма и товар — из payload строки outbox.
    assert "1000,00 KGS" in message.get_content()
    assert "Товар " in message.get_content()
    assert message["Message-ID"] == message_id(dedup_key)

    row = await letter_row(session, dedup_key)
    assert row["status"] == "sent"
    assert row["sent_at"] is not None
    assert row["locked_until"] is None
    await assert_counters_consistent(session, sale_id)


async def test_double_webhook_and_reconciliation_send_one_letter(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
    mailer: FakeMailer,
) -> None:
    """Повторный вебхук и сверка не добавляют второго письма.

    Строка одна (уникальный dedup_key), и отправляется она один раз.
    """
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    _, dedup_key = await pay_and_settle(client, sale_id, token, webhooks=2)

    # Сверка по тому же платежу — уже применено, ничего не добавит.
    await session.execute(
        text("""
            update payments set created_at = now() - interval '1 hour',
                last_checked_at = null, reconcile_attempts = 0
            where status = 'pending'
        """)
    )
    await session.commit()
    await reconcile_once(session, payment_provider)

    assert await letters_with_key(session, dedup_key) == 1, "в outbox больше одной строки"

    await send_outbox_once(session, mailer)
    # Второй проход: письмо уже отправлено, повторять нечего.
    await send_outbox_once(session, mailer)

    assert len(mailer.sent) == 1, f"отправлено писем: {len(mailer.sent)}"
    await assert_counters_consistent(session, sale_id)


async def test_two_senders_send_letter_once(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    """Два отправителя одновременно: SKIP LOCKED не даёт отправить дважды."""
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    _, dedup_key = await pay_and_settle(client, sale_id, token)
    shared = FakeMailer()

    async def participant(participant_session: AsyncSession, _: int) -> object:
        return await send_outbox_once(participant_session, shared)

    run = await run_with_barrier(2, participant)

    errors = [item for item in run.results if isinstance(item, BaseException)]
    assert not errors, f"отправители упали: {errors}"
    ours = [m for m in shared.message_ids() if m == message_id(dedup_key)]
    assert len(ours) == 1, f"письмо отправлено {len(ours)} раз"
    assert (await letter_row(session, dedup_key))["status"] == "sent"
    await assert_counters_consistent(session, sale_id)


async def test_smtp_failure_schedules_retry(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
    mailer: FakeMailer,
) -> None:
    """SMTP недоступен: письмо не потеряно, повтор запланирован позже."""
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    _, dedup_key = await pay_and_settle(client, sale_id, token)
    mailer.fail_times = 1

    report = await send_outbox_once(session, mailer)

    assert report.retried >= 1
    row = await letter_row(session, dedup_key)
    assert row["status"] == "pending", "письмо потеряно"
    assert row["attempts"] == 1
    assert row["retry_in_future"] is True, "повтор должен быть отложен"
    assert row["locked_until"] is None, "аренда должна быть снята"
    assert "тестовый отказ SMTP" in row["last_error"]
    assert mailer.sent == []

    # Пауза прошла, SMTP снова работает — письмо уходит.
    await session.execute(
        text("update outbox set next_attempt_at = now() where dedup_key = :key"),
        {"key": dedup_key},
    )
    await session.commit()
    await send_outbox_once(session, mailer)

    assert len(mailer.sent) == 1
    assert (await letter_row(session, dedup_key))["status"] == "sent"
    await assert_counters_consistent(session, sale_id)


async def test_letter_fails_after_attempt_limit(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
    mailer: FakeMailer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Исчерпание попыток: письмо помечается failed с причиной."""
    monkeypatch.setenv("OUTBOX_MAX_ATTEMPTS", "2")
    get_settings.cache_clear()
    try:
        sale_id = await make_sale(quantity=3)
        token = await login(client)
        _, dedup_key = await pay_and_settle(client, sale_id, token)
        mailer.fail_times = 5

        await send_outbox_once(session, mailer)
        assert (await letter_row(session, dedup_key))["status"] == "pending"

        await session.execute(
            text("update outbox set next_attempt_at = now() where dedup_key = :key"),
            {"key": dedup_key},
        )
        await session.commit()
        await send_outbox_once(session, mailer)

        row = await letter_row(session, dedup_key)
        assert row["status"] == "failed", "после лимита попыток ожидался failed"
        assert row["attempts"] == 2
        assert "тестовый отказ SMTP" in row["last_error"]
        assert row["locked_until"] is None
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()

    await assert_counters_consistent(session, sale_id)


async def test_expired_lease_returns_letter_to_work(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
    mailer: FakeMailer,
) -> None:
    """Строка, занятая упавшим процессом, возвращается в работу."""
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    _, dedup_key = await pay_and_settle(client, sale_id, token)

    # Процесс занял письмо и умер, не отметив результат.
    await session.execute(
        text("""
            update outbox set locked_until = now() + interval '1 hour'
            where dedup_key = :key
        """),
        {"key": dedup_key},
    )
    await session.commit()

    # Пока аренда жива, письмо никто не берёт.
    assert (await send_outbox_once(session, mailer)).claimed == 0
    assert mailer.sent == []

    # Аренда истекла — письмо снова в работе.
    await session.execute(
        text("""
            update outbox set locked_until = now() - interval '1 second'
            where dedup_key = :key
        """),
        {"key": dedup_key},
    )
    await session.commit()

    await send_outbox_once(session, mailer)

    assert len(mailer.sent) == 1
    assert (await letter_row(session, dedup_key))["status"] == "sent"
    await assert_counters_consistent(session, sale_id)


async def test_unknown_kind_fails_without_breaking_the_pass(
    session: AsyncSession, mailer: FakeMailer
) -> None:
    """Неизвестный kind → failed с причиной, остальные письма проходят."""
    dedup_key = f"unknown_kind:{uuid4()}"
    # Заведомо отсутствующий вид письма: все реальные виды есть в реестре
    # TEMPLATES, и добавление нового не должно ломать этот тест.
    await session.execute(
        text("""
            insert into outbox (kind, dedup_key, recipient, payload, status)
            values ('never_implemented_kind', :key, 'buyer@example.com',
                    '{}'::jsonb, 'pending')
        """),
        {"key": dedup_key},
    )
    await session.commit()

    report = await send_outbox_once(session, mailer)

    assert report.failed >= 1
    row = await letter_row(session, dedup_key)
    assert row["status"] == "failed"
    assert "нет шаблона" in row["last_error"]
    assert row["locked_until"] is None
    # Отправлять было нечего, но проход не упал.
    assert mailer.sent == []


async def test_incomplete_payload_fails_with_reason(
    session: AsyncSession, mailer: FakeMailer
) -> None:
    """Неполный payload: повторять бессмысленно, сразу failed."""
    dedup_key = f"order_paid:{uuid4()}"
    await session.execute(
        text("""
            insert into outbox (kind, dedup_key, recipient, payload, status)
            values ('order_paid', :key, 'buyer@example.com',
                    '{"order_id": "x"}'::jsonb, 'pending')
        """),
        {"key": dedup_key},
    )
    await session.commit()

    await send_outbox_once(session, mailer)

    row = await letter_row(session, dedup_key)
    assert row["status"] == "failed"
    assert "нет поля" in row["last_error"]


async def test_smtp_timeout_must_fit_lease() -> None:
    """Таймаут SMTP обязан быть заметно меньше аренды.

    Иначе первый отправитель ещё ждёт SMTP, аренда уже истекла, второй берёт
    ту же строку — и письмо уходит дважды без всякого падения процесса.
    Проверка при старте приложения, а не в рантайме.
    """
    with pytest.raises(ValueError, match="OUTBOX_LEASE_SECONDS"):
        Settings(
            database_url="postgresql+asyncpg://u:p@h/db",
            jwt_secret="x",
            smtp_timeout_seconds=30.0,
            outbox_lease_seconds=30,
        )

    # Двукратный запас — допустимо.
    settings = Settings(
        database_url="postgresql+asyncpg://u:p@h/db",
        jwt_secret="x",
        smtp_timeout_seconds=10.0,
        outbox_lease_seconds=30,
    )
    assert settings.smtp_timeout_seconds == 10.0


async def test_letter_is_built_only_from_payload() -> None:
    """Отправитель не ходит в другие таблицы: письмо собирается из payload."""
    payload = {
        "order_id": "11111111-1111-1111-1111-111111111111",
        "sale_id": "22222222-2222-2222-2222-222222222222",
        "product_name": "Наушники",
        "amount_minor": 499_000,
        "currency": "KGS",
    }

    message = build_message(
        kind="order_paid",
        dedup_key="order_paid:11111111-1111-1111-1111-111111111111",
        recipient="buyer@example.com",
        payload=payload,
    )

    content = message.get_content()
    assert "Наушники" in content
    assert "4990,00 KGS" in content
    assert payload["order_id"] in content
    assert message["To"] == "buyer@example.com"
    assert "Flash Sale" in message["From"]


async def test_empty_outbox_is_noop(session: AsyncSession, mailer: FakeMailer) -> None:
    await session.execute(text("update outbox set status = 'sent' where status = 'pending'"))
    await session.commit()

    report = await send_outbox_once(session, mailer)

    assert report.claimed == 0
    assert mailer.sent == []


# --- проверка против настоящего Mailpit ---------------------------------------


async def mailpit_available() -> str | None:
    """Вернуть причину недоступности Mailpit или None, если он на связи."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(f"{MAILPIT_API}/livez")
    except Exception as error:
        return f"Mailpit недоступен по {MAILPIT_API}: {error}"
    if not response.is_success:
        return f"Mailpit ответил {response.status_code} на /livez"
    return None


async def test_letter_reaches_real_mailpit(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    """Настоящая доставка в Mailpit и проверка через его API.

    Mailpit есть в compose всегда, поэтому тест идёт по умолчанию. Пропуск —
    только если сервис действительно недоступен, с явной причиной.
    """
    reason = await mailpit_available()
    if reason is not None:
        pytest.skip(reason)

    from app.services.mail import SmtpMailer

    sale_id = await make_sale(quantity=3)
    token = await login(client)
    order_id, dedup_key = await pay_and_settle(client, sale_id, token)
    expected_id = message_id(dedup_key)

    report = await send_outbox_once(session, SmtpMailer())
    assert report.sent >= 1, f"письмо не отправлено: {report}"

    async with httpx.AsyncClient(base_url=MAILPIT_API, timeout=5.0) as mailpit:
        # Ищем по Message-ID: ящик общий с другими прогонами и живыми проверками.
        async with asyncio.timeout(10):
            while True:
                found = (
                    await mailpit.get(
                        "/api/v1/search",
                        params={"query": f"message-id:{expected_id.strip('<>')}"},
                    )
                ).json()
                if found["messages_count"] >= 1:
                    break
                await asyncio.sleep(0.2)

        assert found["messages_count"] == 1, (
            f"в Mailpit {found['messages_count']} писем с одним Message-ID"
        )
        summary = found["messages"][0]
        assert summary["Subject"].startswith("Заказ оплачен —")

        full = (await mailpit.get(f"/api/v1/message/{summary['ID']}")).json()
        assert order_id in full["Text"]
        assert "1000,00 KGS" in full["Text"]

    assert (await letter_row(session, dedup_key))["status"] == "sent"
    await assert_counters_consistent(session, sale_id)
