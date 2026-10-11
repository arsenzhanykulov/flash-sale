"""Закрытие распродажи по окончании окна.

Главное, что проверяется: брони в `paying` не снимаются (инвариант 4), письмо
уходит по одному на очищенную корзину, а повторный проход и второй воркер
ничего не добавляют.
"""

import asyncio
import json
from contextlib import suppress
from uuid import UUID, uuid4

from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.payments.flow import idempotency_key_for
from app.services.sale_closer import cart_released_dedup_key, close_sales_once
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
        "/api/auth/login", json={"email": f"closer-{uuid4().hex[:12]}@example.com"}
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


async def end_sale(session: AsyncSession, sale_id: UUID) -> None:
    """Сдвинуть окно распродажи в прошлое, не закрывая её."""
    await session.execute(
        text("""
            update sales
            set start_at = now() - interval '2 hours', end_at = now() - interval '1 second'
            where id = :sale_id
        """),
        {"sale_id": sale_id},
    )
    await session.commit()


async def sale_row(session: AsyncSession, sale_id: UUID) -> dict:
    await session.rollback()
    row = (
        await session.execute(
            text("select quantity, sold, held, closed_at from sales where id = :id"),
            {"id": sale_id},
        )
    ).one()
    return dict(row._mapping)


async def reservation_status(session: AsyncSession, reservation_id: str) -> str:
    await session.rollback()
    return await session.scalar(
        text("select status from reservations where id = :id"), {"id": reservation_id}
    )


async def cart_letters(session: AsyncSession, reservation_id: str) -> list[dict]:
    await session.rollback()
    result = await session.execute(
        text("select kind, recipient, payload from outbox where dedup_key = :key"),
        {"key": cart_released_dedup_key(UUID(reservation_id))},
    )
    return [dict(row._mapping) for row in result]


async def send_webhook(client: AsyncClient, reservation_id: str, status: str) -> Response:
    body = json.dumps(
        {
            "payment_id": "stub-close",
            "idempotency_key": idempotency_key_for(UUID(reservation_id)),
            "status": status,
        },
        separators=(",", ":"),
    ).encode()
    return await client.post("/api/payments/webhook", content=body, headers=webhook_headers(body))


async def test_closing_releases_holds_and_sends_letters(
    client: AsyncClient, session: AsyncSession, make_sale: MakeSale
) -> None:
    """Корзины очищены, held = 0, по письму на корзину, closed_at заполнен."""
    sale_id = await make_sale(quantity=5)
    first = await reserve(client, sale_id, await login(client))
    second = await reserve(client, sale_id, await login(client))
    _, _, held_before = await read_counters(session, sale_id)
    assert held_before == 2
    await end_sale(session, sale_id)

    report = await close_sales_once(session)

    assert report.released >= 2, f"отчёт: {report}"
    sale = await sale_row(session, sale_id)
    assert sale["held"] == 0
    assert sale["closed_at"] is not None
    # quantity не трогаем: магазину нужна цифра «не продано».
    assert sale["quantity"] == 5
    assert sale["sold"] == 0

    for reservation in (first, second):
        assert await reservation_status(session, reservation["id"]) == "cancelled"
        letters = await cart_letters(session, reservation["id"])
        assert len(letters) == 1, f"писем на корзину: {len(letters)}"
        assert letters[0]["kind"] == "sale_closed_cart_released"
        assert letters[0]["payload"]["product_name"].startswith("Товар ")

    await assert_counters_consistent(session, sale_id)


async def test_paying_reservation_is_not_released_and_can_still_succeed(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    """Инвариант 4: paying не снимается, успешная оплата после закрытия → sold + 1."""
    sale_id = await make_sale(quantity=5)
    paying_token = await login(client)
    paying = await reserve(client, sale_id, paying_token)
    order = (await pay(client, paying["id"], paying_token)).json()
    # Вторая корзина — обычный held, её снимут.
    held = await reserve(client, sale_id, await login(client))
    await end_sale(session, sale_id)

    await close_sales_once(session)

    assert await reservation_status(session, paying["id"]) == "paying", "paying снята"
    assert await reservation_status(session, held["id"]) == "cancelled"
    # Письмо об очищенной корзине — только владельцу held.
    assert len(await cart_letters(session, held["id"])) == 1
    assert await cart_letters(session, paying["id"]) == []
    sale = await sale_row(session, sale_id)
    assert (sale["sold"], sale["held"]) == (0, 1)
    assert sale["closed_at"] is not None

    # Оплата доводится до конца уже после закрытия распродажи.
    webhook = await send_webhook(client, paying["id"], "succeeded")
    assert webhook.status_code == 200
    assert webhook.json()["applied"] is True

    assert await reservation_status(session, paying["id"]) == "paid"
    order_status = await session.scalar(
        text("select status from orders where id = :id"), {"id": order["order_id"]}
    )
    assert order_status == "paid"
    sale = await sale_row(session, sale_id)
    assert (sale["sold"], sale["held"]) == (1, 0)
    await assert_counters_consistent(session, sale_id)


async def test_two_closers_send_one_letter_per_cart(
    client: AsyncClient, session: AsyncSession, make_sale: MakeSale
) -> None:
    """Два воркера одновременно: по одному письму на корзину."""
    sale_id = await make_sale(quantity=5)
    reservations = [await reserve(client, sale_id, await login(client)) for _ in range(2)]
    await end_sale(session, sale_id)

    async def participant(participant_session: AsyncSession, _: int) -> object:
        return await close_sales_once(participant_session)

    run = await run_with_barrier(2, participant)

    errors = [item for item in run.results if isinstance(item, BaseException)]
    assert not errors, f"воркеры упали: {errors}"

    for reservation in reservations:
        letters = await cart_letters(session, reservation["id"])
        assert len(letters) == 1, f"писем на корзину: {len(letters)}"
    sale = await sale_row(session, sale_id)
    assert sale["held"] == 0, f"held={sale['held']}"
    await assert_counters_consistent(session, sale_id)


async def test_pay_after_end_at_is_refused(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    """«Оплатить» после end_at, пока воркер ещё не прошёл → 409 sale_ended."""
    sale_id = await make_sale(quantity=5)
    token = await login(client)
    reservation = await reserve(client, sale_id, token)
    await end_sale(session, sale_id)

    # Воркер ещё не проходил: распродажа открыта, бронь жива.
    assert (await sale_row(session, sale_id))["closed_at"] is None
    assert await reservation_status(session, reservation["id"]) == "held"

    response = await pay(client, reservation["id"], token)

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "sale_ended"
    # Заказ не создан, провайдера не звали.
    assert payment_provider.calls == []
    orders = await session.scalar(
        text("select count(*) from orders where reservation_id = :id"),
        {"id": reservation["id"]},
    )
    assert orders == 0
    await assert_counters_consistent(session, sale_id)


async def test_pay_after_closing_is_refused(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    """После закрытия бронь уже cancelled — отказ по ней."""
    sale_id = await make_sale(quantity=5)
    token = await login(client)
    reservation = await reserve(client, sale_id, token)
    await end_sale(session, sale_id)
    await close_sales_once(session)

    response = await pay(client, reservation["id"], token)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "reservation_cancelled"
    await assert_counters_consistent(session, sale_id)


async def test_closing_and_paying_the_same_reservation_do_not_deadlock(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    """Закрытие и оплата той же брони одновременно: ровно один исход, без дедлоков."""
    for round_number in range(DEADLOCK_ROUNDS):
        sale_id = await make_sale(quantity=3)
        token = await login(client)
        reservation = await reserve(client, sale_id, token)
        await end_sale(session, sale_id)

        async def participant(
            participant_session: AsyncSession,
            index: int,
            # Значения по умолчанию: иначе замыкание взяло бы переменные
            # последней итерации цикла.
            reservation_id: str = reservation["id"],
            buyer_token: str = token,
        ) -> object:
            if index == 0:
                return await close_sales_once(participant_session)
            return await pay(client, reservation_id, buyer_token)

        run = await run_with_barrier(2, participant)

        deadlocks = [
            item
            for item in run.results
            if isinstance(item, BaseException) and "deadlock" in str(item).lower()
        ]
        assert not deadlocks, f"раунд {round_number}: {deadlocks}"

        # Ровно один исход: либо бронь снята закрытием, либо оплата началась.
        status = await reservation_status(session, reservation["id"])
        assert status in {"cancelled", "paying"}, f"раунд {round_number}: статус {status}"
        await assert_counters_consistent(session, sale_id)


async def test_running_sale_is_untouched(
    client: AsyncClient, session: AsyncSession, make_sale: MakeSale
) -> None:
    """Незакончившаяся распродажа не трогается."""
    sale_id = await make_sale(quantity=5, end_at="now() + interval '1 hour'")
    reservation = await reserve(client, sale_id, await login(client))

    await close_sales_once(session)

    sale = await sale_row(session, sale_id)
    assert sale["closed_at"] is None
    assert sale["held"] == 1
    assert await reservation_status(session, reservation["id"]) == "held"
    assert await cart_letters(session, reservation["id"]) == []
    await assert_counters_consistent(session, sale_id)


async def test_second_pass_adds_nothing(
    client: AsyncClient, session: AsyncSession, make_sale: MakeSale
) -> None:
    """Повторный проход по закрытой распродаже не добавляет писем."""
    sale_id = await make_sale(quantity=5)
    reservation = await reserve(client, sale_id, await login(client))
    await end_sale(session, sale_id)

    await close_sales_once(session)
    first_closed_at = (await sale_row(session, sale_id))["closed_at"]

    await close_sales_once(session)

    assert len(await cart_letters(session, reservation["id"])) == 1
    assert (await sale_row(session, sale_id))["closed_at"] == first_closed_at
    assert (await sale_row(session, sale_id))["held"] == 0
    await assert_counters_consistent(session, sale_id)


async def test_closing_keeps_quantity_for_unsold_count(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    """quantity не уменьшается: «не продано» = quantity - sold.

    Магазину нужна эта цифра после закрытия, поэтому закрытие трогает только
    held и closed_at.
    """
    sale_id = await make_sale(quantity=5)
    # Одна единица продана по-настоящему: счётчик sold должен быть честным,
    # иначе assert_counters_consistent справедливо поймает расхождение.
    buyer = await login(client)
    sold_reservation = await reserve(client, sale_id, buyer)
    await pay(client, sold_reservation["id"], buyer)
    assert (await send_webhook(client, sold_reservation["id"], "succeeded")).status_code == 200

    await end_sale(session, sale_id)
    await close_sales_once(session)

    sale = await sale_row(session, sale_id)
    assert sale["closed_at"] is not None
    assert (sale["sold"], sale["held"]) == (1, 0)
    assert sale["quantity"] == 5, "quantity не должен меняться при закрытии"
    assert sale["quantity"] - sale["sold"] == 4, "не продано"
    await assert_counters_consistent(session, sale_id)


async def test_websocket_receives_ended_status_after_closing(
    session: AsyncSession, make_sale: MakeSale
) -> None:
    """Закрытие даёт live-обновление само: триггер следит за closed_at (ADR-020)."""
    import app.api.routes.live as live_route
    from app.services.live_updates import SaleChangeListener, SaleHub
    from tests.test_live_updates import RECEIVE_TIMEOUT, open_ws, receive_snapshot
    from tests.test_live_updates import start_listener as _  # noqa: F401

    original_hub = live_route.hub
    test_hub = SaleHub()
    live_route.hub = test_hub
    listener = SaleChangeListener(test_hub, window_ms=0)
    task = asyncio.create_task(listener.run(), name="test-live-closer")
    try:
        async with asyncio.timeout(RECEIVE_TIMEOUT):
            while listener.backend_pid is None:
                await asyncio.sleep(0.01)

        sale_id = await make_sale(quantity=5)

        async with open_ws(f"/ws/sales/{sale_id}") as websocket:
            assert (await receive_snapshot(websocket))["status"] == "active"

            await end_sale(session, sale_id)
            await close_sales_once(session)

            # Снимков может прийти несколько (end_at, затем closed_at) —
            # ждём того, в котором статус уже ended.
            async with asyncio.timeout(RECEIVE_TIMEOUT):
                while True:
                    update = await receive_snapshot(websocket)
                    if update["status"] == "ended":
                        break

        assert update["status"] == "ended"
    finally:
        live_route.hub = original_hub
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    await assert_counters_consistent(session, sale_id)
