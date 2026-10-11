"""Оплата брони: заказ, платёж, вебхук, outbox.

Настоящая заглушка здесь не поднимается — провайдер подменён фейком (ADR-021).
Проверяется наша половина: переходы статусов, идемпотентность, порядок
блокировок и то, что письмо кладётся в outbox ровно один раз.
"""

import asyncio
import json
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PaymentStatus
from app.services.payments import apply_payment_result, find_payment_by_idempotency_key
from app.services.payments.flow import idempotency_key_for, order_paid_dedup_key
from app.services.reservations import cancel_reservation, create_reservation
from app.services.sweeper import expire_holds_of_sale
from tests.conftest import (
    FakePaymentProvider,
    MakeSale,
    assert_counters_consistent,
    read_counters,
    run_with_barrier,
    webhook_headers,
)

DEADLOCK_ROUNDS = 20


async def login(client: AsyncClient) -> str:
    response = await client.post(
        "/api/auth/login", json={"email": f"payer-{uuid4().hex[:12]}@example.com"}
    )
    return response.json()["access_token"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def reserve(client: AsyncClient, sale_id: UUID, token: str) -> dict:
    response = await client.post(f"/api/sales/{sale_id}/reservations", headers=auth(token))
    assert response.status_code == 201, response.text
    return response.json()


async def pay(client: AsyncClient, reservation_id: str, token: str) -> Response:
    return await client.post(f"/api/reservations/{reservation_id}/pay", headers=auth(token))


async def send_webhook(
    client: AsyncClient,
    *,
    idempotency_key: str,
    status: str,
    provider_payment_id: str = "stub-1",
    signature: str | None = None,
) -> Response:
    body = json.dumps(
        {
            "payment_id": provider_payment_id,
            "idempotency_key": idempotency_key,
            "status": status,
        },
        separators=(",", ":"),
    ).encode()
    headers = webhook_headers(body)
    if signature is not None:
        headers["X-Signature"] = signature
    return await client.post("/api/payments/webhook", content=body, headers=headers)


async def row_of(session: AsyncSession, sql: str, params: dict) -> dict | None:
    await session.rollback()
    row = (await session.execute(text(sql), params)).one_or_none()
    return None if row is None else dict(row._mapping)


async def outbox_rows(session: AsyncSession, dedup_key: str) -> list[dict]:
    await session.rollback()
    result = await session.execute(
        text("select kind, recipient, payload, status from outbox where dedup_key = :key"),
        {"key": dedup_key},
    )
    return [dict(row._mapping) for row in result]


async def test_successful_payment_updates_all_tables(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    reservation = await reserve(client, sale_id, token)

    paid = await pay(client, reservation["id"], token)

    assert paid.status_code == 200, paid.text
    body = paid.json()
    assert body["order_status"] == "pending_payment"
    assert body["payment_status"] == "pending"
    assert body["provider_state"] == "accepted"
    assert body["already_started"] is False
    # Сумма взята из цены распродажи.
    assert body["amount_minor"] == 100000

    # Провайдер получил наш детерминированный ключ и адрес вебхука.
    assert len(payment_provider.calls) == 1
    assert payment_provider.calls[0].idempotency_key == idempotency_key_for(UUID(reservation["id"]))
    assert payment_provider.calls[0].callback_url.endswith("/api/payments/webhook")

    # Пока результата нет: единица удержана, не продана.
    _, sold, held = await read_counters(session, sale_id)
    assert (sold, held) == (0, 1)

    webhook = await send_webhook(
        client,
        idempotency_key=body["payment_id"] and idempotency_key_for(UUID(reservation["id"])),
        status="succeeded",
    )
    assert webhook.status_code == 200
    assert webhook.json()["applied"] is True

    # 1. платёж
    payment = await row_of(
        session,
        "select status, provider_payment_id from payments where id = :id",
        {"id": body["payment_id"]},
    )
    assert payment["status"] == "succeeded"
    assert payment["provider_payment_id"] is not None

    # 2. заказ
    order = await row_of(
        session,
        "select status, paid_at, amount_minor from orders where id = :id",
        {"id": body["order_id"]},
    )
    assert order["status"] == "paid"
    assert order["paid_at"] is not None

    # 3. бронь
    reservation_row = await row_of(
        session, "select status from reservations where id = :id", {"id": reservation["id"]}
    )
    assert reservation_row["status"] == "paid"

    # 4. распродажа: единица продана, а не удержана
    _, sold, held = await read_counters(session, sale_id)
    assert (sold, held) == (1, 0)

    # 5. outbox: ровно одна строка
    rows = await outbox_rows(session, order_paid_dedup_key(UUID(body["order_id"])))
    assert len(rows) == 1
    assert rows[0]["kind"] == "order_paid"
    assert rows[0]["status"] == "pending"
    assert rows[0]["payload"]["order_id"] == body["order_id"]
    assert rows[0]["recipient"].endswith("@example.com")

    await assert_counters_consistent(session, sale_id)


async def test_declined_payment_returns_unit(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    reservation = await reserve(client, sale_id, token)
    body = (await pay(client, reservation["id"], token)).json()

    await send_webhook(
        client,
        idempotency_key=idempotency_key_for(UUID(reservation["id"])),
        status="declined",
    )

    order = await row_of(
        session, "select status from orders where id = :id", {"id": body["order_id"]}
    )
    assert order["status"] == "failed"
    reservation_row = await row_of(
        session, "select status from reservations where id = :id", {"id": reservation["id"]}
    )
    assert reservation_row["status"] == "failed"

    # Единица вернулась на витрину.
    quantity, sold, held = await read_counters(session, sale_id)
    assert (sold, held) == (0, 0)
    assert quantity - sold - held == 3

    # Письма об оплате быть не должно.
    assert await outbox_rows(session, order_paid_dedup_key(UUID(body["order_id"]))) == []
    await assert_counters_consistent(session, sale_id)


async def test_double_pay_creates_one_order(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    """Двойной «Оплатить» одновременно: один заказ, один платёж."""
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    reservation = await reserve(client, sale_id, token)

    async def attempt(_: AsyncSession, __: int) -> Response:
        return await pay(client, reservation["id"], token)

    run = await run_with_barrier(2, attempt)

    codes = sorted(response.status_code for response in run.results)
    assert codes == [200, 200], f"коды ответов: {codes}"
    order_ids = {response.json()["order_id"] for response in run.results}
    assert len(order_ids) == 1, f"создано заказов: {len(order_ids)}"

    counts = await row_of(
        session,
        """
        select
            (select count(*) from orders where reservation_id = :reservation_id) as orders,
            (select count(*) from payments p join orders o on o.id = p.order_id
             where o.reservation_id = :reservation_id) as payments
        """,
        {"reservation_id": reservation["id"]},
    )
    assert (counts["orders"], counts["payments"]) == (1, 1)
    # Один из ответов — повторный, и это видно.
    assert {response.json()["already_started"] for response in run.results} == {False, True}
    await assert_counters_consistent(session, sale_id)


async def test_double_webhook_sells_unit_once(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    """Два одновременных вебхука: sold + 1 один раз, одна строка outbox."""
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    reservation = await reserve(client, sale_id, token)
    body = (await pay(client, reservation["id"], token)).json()
    key = idempotency_key_for(UUID(reservation["id"]))

    async def attempt(_: AsyncSession, __: int) -> Response:
        return await send_webhook(client, idempotency_key=key, status="succeeded")

    run = await run_with_barrier(2, attempt)

    assert sorted(response.status_code for response in run.results) == [200, 200]
    # Применён ровно один раз: второй вебхук получил applied=False.
    applied = [response.json()["applied"] for response in run.results]
    assert sorted(applied) == [False, True], f"applied: {applied}"

    _, sold, held = await read_counters(session, sale_id)
    assert (sold, held) == (1, 0), f"sold={sold}, held={held}"
    rows = await outbox_rows(session, order_paid_dedup_key(UUID(body["order_id"])))
    assert len(rows) == 1, f"строк outbox: {len(rows)}"
    await assert_counters_consistent(session, sale_id)


async def test_paying_reservation_survives_sweeper(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    """Инвариант 4: оплата начата за секунду до истечения, свипер прошёл после.

    Бронь не истекает, и вебхук спокойно завершает заказ.
    """
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    reservation = await reserve(client, sale_id, token)

    # Срок — через секунду: оплата успевает начаться.
    await session.execute(
        text("""
            update reservations set expires_at = now() + interval '1 second'
            where id = :id
        """),
        {"id": reservation["id"]},
    )
    await session.commit()

    body = (await pay(client, reservation["id"], token)).json()
    assert body["order_status"] == "pending_payment"

    # Срок прошёл — свипер обязан пройти мимо: статус уже paying.
    await session.execute(
        text("update reservations set expires_at = now() - interval '1 second' where id = :id"),
        {"id": reservation["id"]},
    )
    await session.commit()
    # Именно по этой распродаже: sweep_expired_once идёт по всей базе,
    # где лежат брони других тестов, и общее число ничего не доказывало бы.
    assert await expire_holds_of_sale(session, sale_id) == 0, "свипер тронул бронь в paying"

    reservation_row = await row_of(
        session, "select status from reservations where id = :id", {"id": reservation["id"]}
    )
    assert reservation_row["status"] == "paying"

    webhook = await send_webhook(
        client,
        idempotency_key=idempotency_key_for(UUID(reservation["id"])),
        status="succeeded",
    )
    assert webhook.json()["applied"] is True

    order = await row_of(
        session, "select status from orders where id = :id", {"id": body["order_id"]}
    )
    assert order["status"] == "paid"
    _, sold, held = await read_counters(session, sale_id)
    assert (sold, held) == (1, 0)
    await assert_counters_consistent(session, sale_id)


async def test_webhook_completes_order_after_sale_window_closed(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    """end_at распродажи прошёл, пока бронь в paying — заказ всё равно завершается.

    Начатая оплата доводится до конца (инвариант 4), даже если распродажа
    закончилась. Тест должен пережить появление закрытия распродажи воркером.
    """
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    reservation = await reserve(client, sale_id, token)
    body = (await pay(client, reservation["id"], token)).json()

    # Окно распродажи закрылось уже после начала оплаты.
    await session.execute(
        text("""
            update sales
            set start_at = now() - interval '2 hours', end_at = now() - interval '1 second'
            where id = :sale_id
        """),
        {"sale_id": sale_id},
    )
    await session.commit()

    webhook = await send_webhook(
        client,
        idempotency_key=idempotency_key_for(UUID(reservation["id"])),
        status="succeeded",
    )

    assert webhook.status_code == 200
    assert webhook.json()["applied"] is True
    order = await row_of(
        session, "select status from orders where id = :id", {"id": body["order_id"]}
    )
    assert order["status"] == "paid"
    _, sold, held = await read_counters(session, sale_id)
    assert (sold, held) == (1, 0)
    await assert_counters_consistent(session, sale_id)


async def test_pay_after_expiry_is_refused(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    reservation = await reserve(client, sale_id, token)
    await session.execute(
        text("update reservations set expires_at = now() - interval '1 minute' where id = :id"),
        {"id": reservation["id"]},
    )
    await session.commit()

    response = await pay(client, reservation["id"], token)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "reservation_expired"
    # Заказ не создавался, провайдера не звали.
    assert payment_provider.calls == []
    counts = await row_of(
        session,
        "select count(*) as orders from orders where reservation_id = :id",
        {"id": reservation["id"]},
    )
    assert counts["orders"] == 0
    await assert_counters_consistent(session, sale_id)


async def test_provider_unavailable_is_not_500(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    """Платёжная система недоступна: заказ создан и ждёт сверки."""
    payment_provider.unavailable = True
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    reservation = await reserve(client, sale_id, token)

    response = await pay(client, reservation["id"], token)

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["provider_state"] == "awaiting_provider"

    order = await row_of(
        session, "select status from orders where id = :id", {"id": body["order_id"]}
    )
    assert order["status"] == "pending_payment"
    payment = await row_of(
        session,
        "select status, provider_payment_id from payments where id = :id",
        {"id": body["payment_id"]},
    )
    assert payment["status"] == "pending"
    assert payment["provider_payment_id"] is None
    # Бронь в paying, единица удержана.
    _, sold, held = await read_counters(session, sale_id)
    assert (sold, held) == (0, 1)
    await assert_counters_consistent(session, sale_id)


async def test_webhook_completes_order_when_create_response_was_lost(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    """Заглушка создала платёж, а ответ не дошёл — вебхук всё равно завершает заказ.

    Поиск идёт по НАШЕМУ idempotency_key: provider_payment_id в этот момент
    пустой, по нему платёж найти было бы нельзя. Из вебхука он дозаписывается.
    """
    payment_provider.unavailable = True
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    reservation = await reserve(client, sale_id, token)
    body = (await pay(client, reservation["id"], token)).json()
    assert body["provider_state"] == "awaiting_provider"

    webhook = await send_webhook(
        client,
        idempotency_key=idempotency_key_for(UUID(reservation["id"])),
        status="succeeded",
        provider_payment_id="stub-lost-response",
    )

    assert webhook.status_code == 200
    assert webhook.json()["applied"] is True
    payment = await row_of(
        session,
        "select status, provider_payment_id from payments where id = :id",
        {"id": body["payment_id"]},
    )
    assert payment["status"] == "succeeded"
    assert payment["provider_payment_id"] == "stub-lost-response", (
        "provider_payment_id должен дозаписаться из вебхука"
    )
    _, sold, held = await read_counters(session, sale_id)
    assert (sold, held) == (1, 0)
    await assert_counters_consistent(session, sale_id)


async def test_webhook_with_bad_signature_is_401(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    reservation = await reserve(client, sale_id, token)
    body = (await pay(client, reservation["id"], token)).json()

    response = await send_webhook(
        client,
        idempotency_key=idempotency_key_for(UUID(reservation["id"])),
        status="succeeded",
        signature="sha256=deadbeef",
    )

    assert response.status_code == 401
    # Ничего не изменилось.
    payment = await row_of(
        session, "select status from payments where id = :id", {"id": body["payment_id"]}
    )
    assert payment["status"] == "pending"
    order = await row_of(
        session, "select status from orders where id = :id", {"id": body["order_id"]}
    )
    assert order["status"] == "pending_payment"
    _, sold, held = await read_counters(session, sale_id)
    assert (sold, held) == (0, 1)
    assert await outbox_rows(session, order_paid_dedup_key(UUID(body["order_id"]))) == []
    await assert_counters_consistent(session, sale_id)


async def test_webhook_without_signature_is_401(client: AsyncClient) -> None:
    response = await client.post(
        "/api/payments/webhook",
        content=b'{"payment_id":"x","idempotency_key":"y","status":"succeeded"}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 401


async def test_webhook_for_unknown_payment_is_404(client: AsyncClient) -> None:
    response = await send_webhook(client, idempotency_key=f"pay:{uuid4()}", status="succeeded")
    assert response.status_code == 404


async def test_pay_requires_login(client: AsyncClient) -> None:
    response = await client.post(f"/api/reservations/{uuid4()}/pay")
    assert response.status_code == 401


async def test_paying_someone_elses_reservation_is_404(
    client: AsyncClient, make_sale: MakeSale, payment_provider: FakePaymentProvider
) -> None:
    sale_id = await make_sale(quantity=3)
    owner = await login(client)
    stranger = await login(client)
    reservation = await reserve(client, sale_id, owner)

    response = await pay(client, reservation["id"], stranger)

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "reservation_not_found"
    assert payment_provider.calls == []


async def test_pay_cancelled_reservation_is_refused(
    client: AsyncClient, make_sale: MakeSale, payment_provider: FakePaymentProvider
) -> None:
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    reservation = await reserve(client, sale_id, token)
    assert (
        await client.delete(f"/api/reservations/{reservation['id']}", headers=auth(token))
    ).status_code == 204

    response = await pay(client, reservation["id"], token)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "reservation_cancelled"


async def test_webhook_with_pending_status_changes_nothing(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    reservation = await reserve(client, sale_id, token)
    body = (await pay(client, reservation["id"], token)).json()

    response = await send_webhook(
        client,
        idempotency_key=idempotency_key_for(UUID(reservation["id"])),
        status="pending",
    )

    assert response.status_code == 200
    assert response.json()["applied"] is False
    payment = await row_of(
        session, "select status from payments where id = :id", {"id": body["payment_id"]}
    )
    assert payment["status"] == "pending"
    await assert_counters_consistent(session, sale_id)


async def test_apply_payment_result_is_reusable_by_reconciliation(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    """Та же функция, что зовёт вебхук, применима и из сверки (шаг 9)."""
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    reservation = await reserve(client, sale_id, token)
    body = (await pay(client, reservation["id"], token)).json()

    await session.rollback()
    payment = await find_payment_by_idempotency_key(
        session, idempotency_key_for(UUID(reservation["id"]))
    )
    assert payment is not None

    applied = await apply_payment_result(session, payment, PaymentStatus.SUCCEEDED)
    assert applied is True
    # Повторный вызов ничего не меняет.
    await session.rollback()
    again = await find_payment_by_idempotency_key(
        session, idempotency_key_for(UUID(reservation["id"]))
    )
    assert again is not None
    assert await apply_payment_result(session, again, PaymentStatus.SUCCEEDED) is False

    _, sold, held = await read_counters(session, sale_id)
    assert (sold, held) == (1, 0)
    assert len(await outbox_rows(session, order_paid_dedup_key(UUID(body["order_id"])))) == 1
    await assert_counters_consistent(session, sale_id)


async def test_webhook_does_not_deadlock_with_reserve_and_cancel(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    """Инвариант 10: вебхук, резерв и отмена на одной распродаже — без дедлоков."""
    sale_id = await make_sale(quantity=DEADLOCK_ROUNDS * 3)

    for round_number in range(DEADLOCK_ROUNDS):
        payer = await login(client)
        reservation = await reserve(client, sale_id, payer)
        await pay(client, reservation["id"], payer)
        key = idempotency_key_for(UUID(reservation["id"]))

        # Участник, чью бронь будет отменять третий.
        canceller_token = await login(client)
        to_cancel = await reserve(client, sale_id, canceller_token)
        canceller_id = await session.scalar(
            text("select user_id from reservations where id = :id"), {"id": to_cancel["id"]}
        )
        reserver_id = await session.scalar(
            text("insert into users (email, role) values (:email, 'buyer') returning id"),
            {"email": f"racer-{uuid4().hex[:12]}@example.com"},
        )
        await session.commit()

        async def participant(
            participant_session: AsyncSession,
            index: int,
            key: str = key,
            cancel_id: str = to_cancel["id"],
            canceller: UUID = canceller_id,
            reserver: UUID = reserver_id,
        ) -> object:
            if index == 0:
                return await send_webhook(client, idempotency_key=key, status="succeeded")
            if index == 1:
                return await cancel_reservation(
                    participant_session, reservation_id=UUID(cancel_id), user_id=canceller
                )
            return await create_reservation(participant_session, sale_id=sale_id, user_id=reserver)

        run = await run_with_barrier(3, participant)

        database_errors = [
            item
            for item in run.results
            if isinstance(item, Exception) and "deadlock" in str(item).lower()
        ]
        assert not database_errors, f"раунд {round_number}: {database_errors}"
        await assert_counters_consistent(session, sale_id)


@pytest.mark.parametrize("status", ["succeeded", "declined"])
async def test_webhook_is_idempotent_across_repeats(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
    status: str,
) -> None:
    """Заглушка с duplicate_webhooks присылает результат дважды последовательно."""
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    reservation = await reserve(client, sale_id, token)
    body = (await pay(client, reservation["id"], token)).json()
    key = idempotency_key_for(UUID(reservation["id"]))

    first = await send_webhook(client, idempotency_key=key, status=status)
    second = await send_webhook(client, idempotency_key=key, status=status)

    assert first.json()["applied"] is True
    assert second.json()["applied"] is False
    assert second.status_code == 200, "провайдер должен получить 200 и перестать повторять"

    _, sold, held = await read_counters(session, sale_id)
    expected = (1, 0) if status == "succeeded" else (0, 0)
    assert (sold, held) == expected
    rows = await outbox_rows(session, order_paid_dedup_key(UUID(body["order_id"])))
    assert len(rows) == (1 if status == "succeeded" else 0)
    await assert_counters_consistent(session, sale_id)


async def test_sweeper_still_ignores_paying(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    """Пункт 5: свипер по-прежнему не трогает paying — проверяем после правок."""
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    reservation = await reserve(client, sale_id, token)
    await pay(client, reservation["id"], token)
    await session.execute(
        text("update reservations set expires_at = now() - interval '1 hour' where id = :id"),
        {"id": reservation["id"]},
    )
    await session.commit()

    released = await expire_holds_of_sale(session, sale_id)

    assert released == 0
    row = await row_of(
        session, "select status from reservations where id = :id", {"id": reservation["id"]}
    )
    assert row["status"] == "paying"
    _, sold, held = await read_counters(session, sale_id)
    assert (sold, held) == (0, 1)
    await assert_counters_consistent(session, sale_id)


async def test_concurrent_webhook_and_pay_keep_one_order(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    """Вебхук и повторный «Оплатить» одновременно: состояние остаётся согласованным."""
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    reservation = await reserve(client, sale_id, token)
    await pay(client, reservation["id"], token)
    key = idempotency_key_for(UUID(reservation["id"]))

    async def participant(_: AsyncSession, index: int) -> object:
        if index == 0:
            return await send_webhook(client, idempotency_key=key, status="succeeded")
        return await pay(client, reservation["id"], token)

    run = await run_with_barrier(2, participant)

    assert all(
        isinstance(item, Response) and item.status_code in (200, 202) for item in run.results
    ), f"ответы: {run.results}"
    counts = await row_of(
        session,
        "select count(*) as orders from orders where reservation_id = :id",
        {"id": reservation["id"]},
    )
    assert counts["orders"] == 1
    await assert_counters_consistent(session, sale_id)


async def test_webhook_body_with_extra_fields_is_accepted(
    client: AsyncClient, make_sale: MakeSale, payment_provider: FakePaymentProvider
) -> None:
    """Подпись считается от сырого тела, лишние поля её не ломают."""
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    reservation = await reserve(client, sale_id, token)
    await pay(client, reservation["id"], token)

    body = json.dumps(
        {
            "payment_id": "stub-x",
            "idempotency_key": idempotency_key_for(UUID(reservation["id"])),
            "status": "succeeded",
            "provider_extra": {"attempt": 2},
        },
        separators=(",", ":"),
    ).encode()

    response = await client.post(
        "/api/payments/webhook", content=body, headers=webhook_headers(body)
    )

    assert response.status_code == 200
    assert response.json()["applied"] is True


async def test_pay_sends_amount_from_sale_price(
    client: AsyncClient,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    reservation = await reserve(client, sale_id, token)

    await pay(client, reservation["id"], token)

    assert payment_provider.calls[0].amount_minor == 100000
    assert payment_provider.calls[0].currency == "KGS"


async def test_webhook_timeout_safety_net(
    client: AsyncClient, make_sale: MakeSale, payment_provider: FakePaymentProvider
) -> None:
    """Повторный «Оплатить» при недоступном провайдере снова его зовёт.

    Так ситуация «транзакция прошла, сеть упала» догоняется без сверки,
    если покупатель нажал ещё раз.
    """
    payment_provider.unavailable = True
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    reservation = await reserve(client, sale_id, token)
    await pay(client, reservation["id"], token)
    assert len(payment_provider.calls) == 1

    payment_provider.unavailable = False
    second = await pay(client, reservation["id"], token)

    assert second.status_code == 200
    assert second.json()["already_started"] is True
    assert len(payment_provider.calls) == 2
    # Ключ тот же — заглушка не создаст второй платёж.
    assert payment_provider.calls[0].idempotency_key == payment_provider.calls[1].idempotency_key


async def test_outbox_payload_has_product_and_amount(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    reservation = await reserve(client, sale_id, token)
    body = (await pay(client, reservation["id"], token)).json()

    await send_webhook(
        client,
        idempotency_key=idempotency_key_for(UUID(reservation["id"])),
        status="succeeded",
    )

    rows = await outbox_rows(session, order_paid_dedup_key(UUID(body["order_id"])))
    payload = rows[0]["payload"]
    assert payload["amount_minor"] == 100000
    assert payload["currency"] == "KGS"
    assert payload["product_name"].startswith("Товар ")
    assert payload["sale_id"] == str(sale_id)


async def test_pay_is_rejected_twice_after_decline(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    reservation = await reserve(client, sale_id, token)
    await pay(client, reservation["id"], token)
    await send_webhook(
        client,
        idempotency_key=idempotency_key_for(UUID(reservation["id"])),
        status="declined",
    )

    response = await pay(client, reservation["id"], token)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "payment_failed"
    await assert_counters_consistent(session, sale_id)


async def test_many_payers_cannot_oversell(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    """Все брони распродажи оплачены одновременно: sold не превышает quantity."""
    quantity = 5
    sale_id = await make_sale(quantity=quantity)

    reservations = []
    for _ in range(quantity):
        token = await login(client)
        reservation = await reserve(client, sale_id, token)
        await pay(client, reservation["id"], token)
        reservations.append(reservation["id"])

    async def attempt(_: AsyncSession, index: int) -> Response:
        return await send_webhook(
            client,
            idempotency_key=idempotency_key_for(UUID(reservations[index])),
            status="succeeded",
        )

    run = await run_with_barrier(quantity, attempt)

    assert all(response.status_code == 200 for response in run.results)
    quantity_db, sold, held = await read_counters(session, sale_id)
    assert (sold, held) == (quantity, 0)
    assert sold <= quantity_db
    await assert_counters_consistent(session, sale_id)


async def test_webhook_json_garbage_is_422(client: AsyncClient) -> None:
    body = b"not json at all"
    response = await client.post(
        "/api/payments/webhook", content=body, headers=webhook_headers(body)
    )
    assert response.status_code == 422


async def test_apply_payment_result_rejects_pending(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    """pending — не результат, применять его нельзя."""
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    reservation = await reserve(client, sale_id, token)
    await pay(client, reservation["id"], token)

    await session.rollback()
    payment = await find_payment_by_idempotency_key(
        session, idempotency_key_for(UUID(reservation["id"]))
    )
    assert payment is not None

    with pytest.raises(ValueError):
        await apply_payment_result(session, payment, PaymentStatus.PENDING)

    await asyncio.sleep(0)
    await assert_counters_consistent(session, sale_id)
