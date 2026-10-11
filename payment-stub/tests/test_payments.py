"""Поведение заглушки: идемпотентность, режимы, вебхуки, восстановление."""

import asyncio
from uuid import uuid4

import httpx
import pytest

from app.config import PaymentMode, get_settings
from app.processing import PaymentProcessor
from app.storage import ModeSettings, PaymentStatus, Storage
from tests.conftest import CALLBACK_URL, WebhookReceiver

# Сколько ждать вебхука: он уходит фоновой задачей.
WEBHOOK_TIMEOUT = 3.0


async def set_mode(client: httpx.AsyncClient, mode: str, **extra) -> None:
    response = await client.post("/control/mode", json={"mode": mode, **extra})
    assert response.status_code == 200, response.text


async def wait_for(condition, timeout: float = WEBHOOK_TIMEOUT) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.02)


async def wait_status(
    client: httpx.AsyncClient, payment_id: str, expected: str, timeout: float = WEBHOOK_TIMEOUT
) -> dict:
    async with asyncio.timeout(timeout):
        while True:
            body = (await client.get(f"/payments/{payment_id}")).json()
            if body["status"] == expected:
                return body
            await asyncio.sleep(0.02)


async def test_same_key_returns_same_payment(create_payment) -> None:
    key = f"order-{uuid4().hex[:8]}"

    first = await create_payment(key=key)
    second = await create_payment(key=key)

    assert first.status_code == 202, first.text
    # Повторный вызов не создаёт второй платёж — и это видно по коду.
    assert second.status_code == 200, second.text
    assert first.json()["payment_id"] == second.json()["payment_id"]


async def test_same_key_with_other_amount_is_conflict(create_payment) -> None:
    key = f"order-{uuid4().hex[:8]}"
    await create_payment(key=key, amount_minor=499_000)

    response = await create_payment(key=key, amount_minor=1_000)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "amount_mismatch"


async def test_same_key_with_other_currency_is_conflict(create_payment) -> None:
    key = f"order-{uuid4().hex[:8]}"
    await create_payment(key=key, currency="KGS")

    response = await create_payment(key=key, currency="USD")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "amount_mismatch"


async def test_approve_mode_succeeds_and_sends_webhook(
    client: httpx.AsyncClient, create_payment, receiver: WebhookReceiver
) -> None:
    await set_mode(client, "approve", delay_ms=0)
    created = (await create_payment(key=f"approve-{uuid4().hex[:8]}")).json()

    assert created["status"] == "pending"
    settled = await wait_status(client, created["payment_id"], "succeeded")
    await wait_for(lambda: len(receiver.received) == 1)

    assert settled["webhooks_sent"] == 1
    webhook = receiver.received[0].payload
    assert webhook == {
        "payment_id": created["payment_id"],
        "idempotency_key": created["idempotency_key"],
        "status": "succeeded",
    }


async def test_decline_mode_declines_and_sends_webhook(
    client: httpx.AsyncClient, create_payment, receiver: WebhookReceiver
) -> None:
    await set_mode(client, "decline", delay_ms=0)
    created = (await create_payment(key=f"decline-{uuid4().hex[:8]}")).json()

    await wait_status(client, created["payment_id"], "declined")
    await wait_for(lambda: len(receiver.received) == 1)

    assert receiver.received[0].payload["status"] == "declined"


async def test_webhook_signature_matches(
    client: httpx.AsyncClient, create_payment, receiver: WebhookReceiver
) -> None:
    """Подпись пересчитывается независимо, от принятых байтов."""
    await set_mode(client, "approve", delay_ms=0)
    await create_payment(key=f"signed-{uuid4().hex[:8]}")

    await wait_for(lambda: len(receiver.received) == 1)

    webhook = receiver.received[0]
    assert webhook.signature is not None
    assert webhook.signature.startswith("sha256=")
    assert webhook.signature_matches(get_settings().webhook_secret)
    assert not webhook.signature_matches("wrong-secret")


async def test_hang_mode_keeps_payment_pending(
    client: httpx.AsyncClient, create_payment, receiver: WebhookReceiver
) -> None:
    await set_mode(client, "hang")
    created = (await create_payment(key=f"hang-{uuid4().hex[:8]}")).json()

    await asyncio.sleep(0.2)

    body = (await client.get(f"/payments/{created['payment_id']}")).json()
    assert body["status"] == "pending"
    assert receiver.received == [], "зависший платёж не должен присылать вебхук"


async def test_hanging_payment_can_be_resolved_manually(
    client: httpx.AsyncClient, create_payment, receiver: WebhookReceiver
) -> None:
    await set_mode(client, "hang")
    created = (await create_payment(key=f"resolve-{uuid4().hex[:8]}")).json()

    response = await client.post(
        f"/payments/{created['payment_id']}/resolve", json={"result": "approve"}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"
    await wait_for(lambda: len(receiver.received) == 1)
    assert receiver.received[0].payload["status"] == "succeeded"


async def test_resolving_twice_is_conflict(client: httpx.AsyncClient, create_payment) -> None:
    await set_mode(client, "hang")
    created = (await create_payment(key=f"twice-{uuid4().hex[:8]}")).json()
    payment_id = created["payment_id"]

    first = await client.post(f"/payments/{payment_id}/resolve", json={"result": "approve"})
    second = await client.post(f"/payments/{payment_id}/resolve", json={"result": "decline"})

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "already_resolved"
    # Второе решение не перебило первое.
    assert (await client.get(f"/payments/{payment_id}")).json()["status"] == "succeeded"


async def test_duplicate_webhooks_are_sent_twice(
    client: httpx.AsyncClient, create_payment, receiver: WebhookReceiver
) -> None:
    """Дубль нужен, чтобы проверить идемпотентность приёма на стороне backend."""
    await set_mode(client, "approve", delay_ms=0, duplicate_webhooks=True)
    created = (await create_payment(key=f"dup-{uuid4().hex[:8]}")).json()

    await wait_for(lambda: len(receiver.received) == 2)
    await asyncio.sleep(0.1)

    assert len(receiver.received) == 2, "ожидалось ровно два вебхука"
    assert receiver.received[0].body == receiver.received[1].body
    body = (await client.get(f"/payments/{created['payment_id']}")).json()
    assert body["webhooks_sent"] == 2


async def test_webhook_is_retried_when_receiver_fails(
    client: httpx.AsyncClient, create_payment, receiver: WebhookReceiver
) -> None:
    """Получатель сначала отвечает 500 — заглушка повторяет."""
    receiver.responses = [500, 500, 200]
    await set_mode(client, "approve", delay_ms=0)
    created = (await create_payment(key=f"retry-{uuid4().hex[:8]}")).json()

    await wait_for(lambda: len(receiver.received) == 3)

    body = (await client.get(f"/payments/{created['payment_id']}")).json()
    assert body["webhook_attempts"] == 3, "ожидалось три попытки"
    assert body["webhooks_sent"] == 1, "доставленным считается только успешный ответ"


async def test_webhook_gives_up_after_max_attempts(
    client: httpx.AsyncClient, create_payment, receiver: WebhookReceiver
) -> None:
    """Исчерпание попыток не ломает заглушку: потерю закрывает сверка статуса."""
    receiver.responses = [500]
    await set_mode(client, "approve", delay_ms=0)
    created = (await create_payment(key=f"giveup-{uuid4().hex[:8]}")).json()
    attempts = get_settings().webhook_max_attempts

    await wait_for(lambda: len(receiver.received) == attempts, timeout=5.0)

    body = (await client.get(f"/payments/{created['payment_id']}")).json()
    assert body["webhook_attempts"] == attempts
    assert body["webhooks_sent"] == 0
    # Статус в заглушке всё равно окончательный — backend его доспросит.
    assert body["status"] == "succeeded"


async def test_unknown_payment_is_404(client: httpx.AsyncClient) -> None:
    assert (await client.get(f"/payments/{uuid4()}")).status_code == 404


async def test_resolve_unknown_payment_is_404(client: httpx.AsyncClient) -> None:
    response = await client.post(f"/payments/{uuid4()}/resolve", json={"result": "approve"})
    assert response.status_code == 404


async def test_mode_survives_restart(storage: Storage) -> None:
    """Переключение режима хранится в БД, а не в памяти процесса."""
    await storage.set_mode(
        ModeSettings(mode=PaymentMode.DECLINE, delay_ms=250, duplicate_webhooks=True)
    )
    path = storage.path
    await storage.close()

    reopened = Storage(path)
    await reopened.connect()
    try:
        mode = await reopened.get_mode()
        assert (mode.mode.value, mode.delay_ms, mode.duplicate_webhooks) == (
            "decline",
            250,
            True,
        )
    finally:
        await reopened.close()


async def test_pending_payment_is_finished_after_restart(
    client: httpx.AsyncClient,
    create_payment,
    storage: Storage,
    receiver: WebhookReceiver,
) -> None:
    """Перезапуск во время задержки не оставляет платёж висеть навсегда.

    Фоновая задача живёт в памяти, поэтому срок хранится в БД. Здесь
    перезапуск изображается новым процессором на том же хранилище.
    """
    # Задержка заведомо больше теста: сама задача результат не успеет применить.
    await set_mode(client, "approve", delay_ms=60_000)
    created = (await create_payment(key=f"restart-{uuid4().hex[:8]}")).json()
    payment_id = created["payment_id"]

    # «Падение»: платёж остался pending со сроком в будущем.
    assert (await client.get(f"/payments/{payment_id}")).json()["status"] == "pending"
    # Сдвигаем срок в прошлое — к моменту старта он просрочен.
    await storage.db.execute(
        "update payments set resolve_at = ? where id = ?",
        ("2000-01-01T00:00:00+00:00", payment_id),
    )
    await storage.db.commit()

    # «Старт»: новый процессор доделывает просроченное.
    restarted = PaymentProcessor(storage)
    try:
        recovered = await restarted.recover_pending()
        assert recovered == 1

        await wait_for(lambda: receiver.received != [])
        payment = await storage.get(payment_id)
        assert payment is not None
        assert payment.status is PaymentStatus.SUCCEEDED
        assert receiver.received[0].payload["status"] == "succeeded"
    finally:
        await restarted.shutdown()


async def test_hanging_payment_is_not_touched_after_restart(
    client: httpx.AsyncClient, create_payment, storage: Storage, receiver: WebhookReceiver
) -> None:
    """hang переживает перезапуск и остаётся ждать ручного решения."""
    await set_mode(client, "hang")
    created = (await create_payment(key=f"hang-restart-{uuid4().hex[:8]}")).json()

    restarted = PaymentProcessor(storage)
    try:
        # У hang нет resolve_at, поэтому восстанавливать нечего.
        assert await restarted.recover_pending() == 0
        await asyncio.sleep(0.1)

        payment = await storage.get(created["payment_id"])
        assert payment is not None
        assert payment.status is PaymentStatus.PENDING
        assert receiver.received == []
    finally:
        await restarted.shutdown()


async def test_payments_survive_restart(
    client: httpx.AsyncClient, create_payment, storage: Storage
) -> None:
    """Хранилище файловое: платежи переживают перезапуск процесса."""
    await set_mode(client, "hang")
    created = (await create_payment(key=f"persist-{uuid4().hex[:8]}")).json()
    path = storage.path
    await storage.close()

    reopened = Storage(path)
    await reopened.connect()
    try:
        payment = await reopened.get(created["payment_id"])
        assert payment is not None
        assert payment.idempotency_key == created["idempotency_key"]
    finally:
        await reopened.close()


@pytest.mark.parametrize("mode", ["approve", "decline", "hang"])
async def test_mode_is_recorded_on_payment(
    client: httpx.AsyncClient, create_payment, mode: str
) -> None:
    """В платеже остаётся режим, которым он создан.

    Нужно, чтобы смена режима на лету не меняла судьбу уже созданных платежей.
    """
    await set_mode(client, mode, delay_ms=60_000)
    created = (await create_payment(key=f"{mode}-recorded-{uuid4().hex[:8]}")).json()

    assert created["mode"] == mode


async def test_changing_mode_does_not_affect_existing_payment(
    client: httpx.AsyncClient, create_payment, receiver: WebhookReceiver
) -> None:
    await set_mode(client, "hang")
    created = (await create_payment(key=f"stable-{uuid4().hex[:8]}")).json()

    # Переключаем режим — созданный платёж по-прежнему ждёт ручного решения.
    await set_mode(client, "approve", delay_ms=0)
    await asyncio.sleep(0.2)

    body = (await client.get(f"/payments/{created['payment_id']}")).json()
    assert body["status"] == "pending"
    assert receiver.received == []


async def test_control_page_is_served(client: httpx.AsyncClient) -> None:
    response = await client.get("/")

    assert response.status_code == 200
    assert "Payment stub" in response.text
    assert "approve" in response.text


async def test_callback_url_is_used_as_given(
    client: httpx.AsyncClient, create_payment, receiver: WebhookReceiver
) -> None:
    await set_mode(client, "approve", delay_ms=0)
    await create_payment(key=f"callback-{uuid4().hex[:8]}", callback_url=CALLBACK_URL)

    await wait_for(lambda: len(receiver.received) == 1)

    assert receiver.received[0].payload["status"] == "succeeded"
