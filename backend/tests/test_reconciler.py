"""Сверка зависших платежей.

Провайдер подменён фейком, который на повтор с тем же ключом отдаёт актуальный
статус — как настоящая заглушка (проверено вживую, ADR-023).
"""

from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.services.payments import reconcile_once
from app.services.payments.flow import idempotency_key_for, order_paid_dedup_key
from tests.conftest import (
    FakePaymentProvider,
    MakeSale,
    assert_counters_consistent,
    read_counters,
    run_with_barrier,
    webhook_headers,
)


async def login(client: AsyncClient) -> str:
    response = await client.post(
        "/api/auth/login", json={"email": f"rec-{uuid4().hex[:12]}@example.com"}
    )
    return response.json()["access_token"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def reserve_and_pay(client: AsyncClient, sale_id: UUID, token: str) -> tuple[str, dict]:
    reservation = (
        await client.post(f"/api/sales/{sale_id}/reservations", headers=auth(token))
    ).json()
    order = (
        await client.post(f"/api/reservations/{reservation['id']}/pay", headers=auth(token))
    ).json()
    return reservation["id"], order


async def age_payment(session: AsyncSession, payment_id: str, *, seconds: int = 60) -> None:
    """Сдвинуть платёж в прошлое, чтобы он вышел из льготного периода."""
    await session.execute(
        text("""
            update payments
            set created_at = now() - make_interval(secs => :seconds),
                last_checked_at = null,
                reconcile_attempts = 0
            where id = :payment_id
        """),
        {"payment_id": payment_id, "seconds": seconds},
    )
    await session.commit()


async def payment_row(session: AsyncSession, payment_id: str) -> dict:
    await session.rollback()
    row = (
        await session.execute(
            text("""
                select status, provider_payment_id, last_checked_at, reconcile_attempts
                from payments where id = :id
            """),
            {"id": payment_id},
        )
    ).one()
    return dict(row._mapping)


async def order_status(session: AsyncSession, order_id: str) -> str:
    await session.rollback()
    return await session.scalar(text("select status from orders where id = :id"), {"id": order_id})


async def outbox_count(session: AsyncSession, order_id: str) -> int:
    await session.rollback()
    return await session.scalar(
        text("select count(*) from outbox where dedup_key = :key"),
        {"key": order_paid_dedup_key(UUID(order_id))},
    )


async def test_reconciler_settles_payment_when_webhook_was_lost(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    """Вебхук потерялся, у провайдера уже succeeded — сверка завершает заказ."""
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    reservation_id, order = await reserve_and_pay(client, sale_id, token)
    key = idempotency_key_for(UUID(reservation_id))

    # Провайдер давно решил, а вебхук до нас не дошёл.
    payment_provider.set_status(key, "succeeded")
    await age_payment(session, order["payment_id"])

    report = await reconcile_once(session, payment_provider)

    assert report.settled >= 1, f"отчёт: {report}"
    assert (await payment_row(session, order["payment_id"]))["status"] == "succeeded"
    assert await order_status(session, order["order_id"]) == "paid"
    _, sold, held = await read_counters(session, sale_id)
    assert (sold, held) == (1, 0)
    assert await outbox_count(session, order["order_id"]) == 1
    await assert_counters_consistent(session, sale_id)


async def test_reconciler_creates_payment_when_provider_was_unavailable(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    """Заглушка была недоступна при «Оплатить» — сверка создаёт платёж тем же ключом.

    Переспрос делается тем же POST /payments, поэтому неизвестный провайдеру
    платёж создаётся сейчас, а не теряется.
    """
    payment_provider.unavailable = True
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    reservation_id, order = await reserve_and_pay(client, sale_id, token)
    key = idempotency_key_for(UUID(reservation_id))

    # При оплате провайдер не ответил: id платежа у нас нет.
    assert (await payment_row(session, order["payment_id"]))["provider_payment_id"] is None
    await age_payment(session, order["payment_id"])

    # Провайдер снова на связи и подтверждает платёж.
    payment_provider.unavailable = False
    payment_provider.set_status(key, "succeeded")

    report = await reconcile_once(session, payment_provider)

    assert report.settled >= 1
    row = await payment_row(session, order["payment_id"])
    assert row["status"] == "succeeded"
    assert row["provider_payment_id"] is not None, "id провайдера должен дозаписаться"
    assert await order_status(session, order["order_id"]) == "paid"
    # Ключ тот же, что при первой попытке.
    assert {call.idempotency_key for call in payment_provider.calls_for(key)} == {key}
    _, sold, held = await read_counters(session, sale_id)
    assert (sold, held) == (1, 0)
    await assert_counters_consistent(session, sale_id)


async def test_hanging_payment_stays_pending_and_is_marked(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    """hang: платёж остаётся pending, но отметка о проверке обновляется."""
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    _, order = await reserve_and_pay(client, sale_id, token)
    await age_payment(session, order["payment_id"])
    before = await payment_row(session, order["payment_id"])
    assert before["last_checked_at"] is None

    report = await reconcile_once(session, payment_provider)

    assert report.still_pending >= 1
    after = await payment_row(session, order["payment_id"])
    assert after["status"] == "pending", "заказ не отменяем — ждём сколько угодно"
    assert after["last_checked_at"] is not None
    assert after["reconcile_attempts"] == 1
    # Единица по-прежнему удержана.
    _, sold, held = await read_counters(session, sale_id)
    assert (sold, held) == (0, 1)
    assert await order_status(session, order["order_id"]) == "pending_payment"
    await assert_counters_consistent(session, sale_id)


async def test_hanging_payment_is_settled_after_manual_resolve(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    """После ручного разрешения на стороне заглушки сверка завершает заказ."""
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    reservation_id, order = await reserve_and_pay(client, sale_id, token)
    key = idempotency_key_for(UUID(reservation_id))
    await age_payment(session, order["payment_id"])

    # Первый проход: всё ещё hang.
    assert (await reconcile_once(session, payment_provider)).still_pending >= 1
    assert (await payment_row(session, order["payment_id"]))["status"] == "pending"

    # Платёж отпустили вручную на страничке заглушки.
    payment_provider.set_status(key, "succeeded")
    # Интервал вырос — сдвигаем отметку, чтобы платёж снова попал в выборку.
    await age_payment(session, order["payment_id"])

    report = await reconcile_once(session, payment_provider)

    assert report.settled >= 1
    assert await order_status(session, order["order_id"]) == "paid"
    _, sold, held = await read_counters(session, sale_id)
    assert (sold, held) == (1, 0)
    assert await outbox_count(session, order["order_id"]) == 1
    await assert_counters_consistent(session, sale_id)


async def test_reconciler_and_webhook_sell_unit_once(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    """Сверка и вебхук одновременно: sold + 1 один раз, одна строка outbox."""
    import json

    sale_id = await make_sale(quantity=3)
    token = await login(client)
    reservation_id, order = await reserve_and_pay(client, sale_id, token)
    key = idempotency_key_for(UUID(reservation_id))
    payment_provider.set_status(key, "succeeded")
    await age_payment(session, order["payment_id"])

    body = json.dumps(
        {"payment_id": "stub-race", "idempotency_key": key, "status": "succeeded"},
        separators=(",", ":"),
    ).encode()

    async def participant(participant_session: AsyncSession, index: int) -> object:
        if index == 0:
            return await reconcile_once(participant_session, payment_provider)
        return await client.post(
            "/api/payments/webhook", content=body, headers=webhook_headers(body)
        )

    run = await run_with_barrier(2, participant)

    errors = [item for item in run.results if isinstance(item, BaseException)]
    assert not errors, f"участники упали: {errors}"

    _, sold, held = await read_counters(session, sale_id)
    assert (sold, held) == (1, 0), f"sold={sold}, held={held}"
    assert await outbox_count(session, order["order_id"]) == 1
    assert await order_status(session, order["order_id"]) == "paid"
    await assert_counters_consistent(session, sale_id)


async def test_fresh_payment_is_not_polled(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    """Свежий платёж в льготный период провайдера не беспокоит."""
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    reservation_id, order = await reserve_and_pay(client, sale_id, token)
    key = idempotency_key_for(UUID(reservation_id))
    calls_after_pay = len(payment_provider.calls_for(key))

    # Платёж только что создан — grace его защищает.
    await reconcile_once(session, payment_provider)

    assert len(payment_provider.calls_for(key)) == calls_after_pay, (
        "сверка дёрнула провайдера по свежему платежу"
    )
    # Проверяем именно свой платёж: отчёт мог захватить платежи других тестов.
    assert (await payment_row(session, order["payment_id"]))["last_checked_at"] is None
    await assert_counters_consistent(session, sale_id)


async def test_unavailable_provider_does_not_break_reconciliation(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    """Провайдер недоступен во время сверки: без падения, повтор позже."""
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    _, order = await reserve_and_pay(client, sale_id, token)
    await age_payment(session, order["payment_id"])

    payment_provider.unavailable = True
    report = await reconcile_once(session, payment_provider)

    assert report.unavailable >= 1, f"отчёт: {report}"
    row = await payment_row(session, order["payment_id"])
    assert row["status"] == "pending"
    # Отметка проставлена до сетевого вызова, поэтому повтор будет позже.
    assert row["last_checked_at"] is not None
    assert row["reconcile_attempts"] == 1
    await assert_counters_consistent(session, sale_id)


async def test_declined_result_from_reconciliation_returns_unit(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    reservation_id, order = await reserve_and_pay(client, sale_id, token)
    payment_provider.set_status(idempotency_key_for(UUID(reservation_id)), "declined")
    await age_payment(session, order["payment_id"])

    report = await reconcile_once(session, payment_provider)

    assert report.settled >= 1
    assert await order_status(session, order["order_id"]) == "failed"
    quantity, sold, held = await read_counters(session, sale_id)
    assert (sold, held) == (0, 0)
    assert quantity - sold - held == 3, "единица должна вернуться на витрину"
    assert await outbox_count(session, order["order_id"]) == 0
    await assert_counters_consistent(session, sale_id)


async def test_backoff_prevents_immediate_recheck(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    """Нарастающий интервал: сразу после проверки платёж снова не опрашивается."""
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    reservation_id, order = await reserve_and_pay(client, sale_id, token)
    key = idempotency_key_for(UUID(reservation_id))
    await age_payment(session, order["payment_id"])

    await reconcile_once(session, payment_provider)
    calls_after_first = len(payment_provider.calls_for(key))
    assert calls_after_first >= 1

    # Второй проход сразу же: платёж только что проверен.
    await reconcile_once(session, payment_provider)

    assert len(payment_provider.calls_for(key)) == calls_after_first, (
        "платёж опрошен повторно, не дождавшись интервала"
    )
    assert (await payment_row(session, order["payment_id"]))["reconcile_attempts"] == 1
    await assert_counters_consistent(session, sale_id)


async def test_concurrent_reconcilers_poll_provider_once(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    """Два экземпляра backend: SKIP LOCKED не даёт дёрнуть провайдера дважды.

    Это оптимизация, а не защита корректности — её держит условный UPDATE
    в apply_payment_result (ADR-023).
    """
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    reservation_id, order = await reserve_and_pay(client, sale_id, token)
    key = idempotency_key_for(UUID(reservation_id))
    payment_provider.set_status(key, "succeeded")
    await age_payment(session, order["payment_id"])
    calls_before = len(payment_provider.calls_for(key))

    async def participant(participant_session: AsyncSession, _: int) -> object:
        return await reconcile_once(participant_session, payment_provider)

    run = await run_with_barrier(2, participant)

    errors = [item for item in run.results if isinstance(item, BaseException)]
    assert not errors, f"сверки упали: {errors}"
    polls = len(payment_provider.calls_for(key)) - calls_before
    assert polls == 1, f"провайдера опросили {polls} раз вместо одного"

    _, sold, held = await read_counters(session, sale_id)
    assert (sold, held) == (1, 0)
    assert await outbox_count(session, order["order_id"]) == 1
    await assert_counters_consistent(session, sale_id)


async def test_batch_size_limits_one_pass(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Пачка ограничена лимитом: один проход не забирает всё сразу."""
    monkeypatch.setenv("RECONCILE_BATCH_SIZE", "2")
    get_settings.cache_clear()
    try:
        sale_id = await make_sale(quantity=5)
        payments: list[str] = []
        for _ in range(4):
            token = await login(client)
            _, order = await reserve_and_pay(client, sale_id, token)
            await age_payment(session, order["payment_id"])
            payments.append(order["payment_id"])

        report = await reconcile_once(session, payment_provider)

        assert report.checked == 2, f"за проход взято {report.checked} платежей вместо 2"
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()

    await assert_counters_consistent(session, sale_id)


async def test_reconcile_once_on_empty_database_is_noop(session: AsyncSession) -> None:
    await session.execute(text("update payments set status = 'succeeded' where status = 'pending'"))
    await session.commit()

    report = await reconcile_once(session)

    assert report.checked == 0
    assert report.is_empty


async def test_settled_payment_is_not_polled_again(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    """Завершённый платёж из выборки выпадает."""
    sale_id = await make_sale(quantity=3)
    token = await login(client)
    reservation_id, order = await reserve_and_pay(client, sale_id, token)
    key = idempotency_key_for(UUID(reservation_id))
    payment_provider.set_status(key, "succeeded")
    await age_payment(session, order["payment_id"])
    await reconcile_once(session, payment_provider)
    calls_after_settle = len(payment_provider.calls_for(key))

    # Платёж уже succeeded — сверке он больше не интересен.
    await age_payment(session, order["payment_id"])
    await reconcile_once(session, payment_provider)

    assert len(payment_provider.calls_for(key)) == calls_after_settle
    await assert_counters_consistent(session, sale_id)


async def test_webhook_after_reconciliation_changes_nothing(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    """Запоздавший вебхук после сверки: 200 и никаких изменений."""
    import json

    sale_id = await make_sale(quantity=3)
    token = await login(client)
    reservation_id, order = await reserve_and_pay(client, sale_id, token)
    key = idempotency_key_for(UUID(reservation_id))
    payment_provider.set_status(key, "succeeded")
    await age_payment(session, order["payment_id"])
    await reconcile_once(session, payment_provider)

    body = json.dumps(
        {"payment_id": "stub-late", "idempotency_key": key, "status": "succeeded"},
        separators=(",", ":"),
    ).encode()
    response: Response = await client.post(
        "/api/payments/webhook", content=body, headers=webhook_headers(body)
    )

    assert response.status_code == 200
    assert response.json()["applied"] is False
    _, sold, held = await read_counters(session, sale_id)
    assert (sold, held) == (1, 0)
    assert await outbox_count(session, order["order_id"]) == 1
    await assert_counters_consistent(session, sale_id)
