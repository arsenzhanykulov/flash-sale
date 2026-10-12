"""Дашборд магазина: GET /api/shop/sales.

Проверки — по своим распродажам, а не по всей выдаче: база общая, и соседние
тесты добавляют в неё свои данные (правило из CLAUDE.md).
"""

import json
from uuid import UUID, uuid4

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.payments.flow import idempotency_key_for
from app.services.sale_closer import close_sales_once
from tests.conftest import (
    FakePaymentProvider,
    assert_counters_consistent,
    webhook_headers,
)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def login(client: AsyncClient, email: str) -> str:
    response = await client.post("/api/auth/login", json={"email": email})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


async def make_shop(session: AsyncSession, client: AsyncClient) -> tuple[UUID, str]:
    """Магазин с токеном. Роль ставится в БД: вход создаёт только покупателей."""
    email = f"shop-{uuid4().hex[:12]}@example.com"
    shop_id = await session.scalar(
        text("insert into users (email, role) values (:email, 'shop') returning id"),
        {"email": email},
    )
    await session.commit()
    return shop_id, await login(client, email)


async def make_sale_for(
    session: AsyncSession,
    shop_id: UUID,
    *,
    quantity: int = 5,
    price_minor: int = 100_000,
    end_at: str = "now() + interval '1 hour'",
) -> UUID:
    product_id = await session.scalar(
        text("insert into products (shop_id, name) values (:shop_id, :name) returning id"),
        {"shop_id": shop_id, "name": f"Товар {uuid4().hex[:8]}"},
    )
    sale_id = await session.scalar(
        text(f"""
            insert into sales (product_id, price_minor, currency, quantity, start_at, end_at)
            values (:product_id, :price_minor, 'KGS', :quantity,
                    now() - interval '1 minute', {end_at})
            returning id
        """),
        {"product_id": product_id, "price_minor": price_minor, "quantity": quantity},
    )
    await session.commit()
    return sale_id


async def buy_and_settle(
    client: AsyncClient, sale_id: UUID, status: str | None = "succeeded"
) -> dict:
    """Бронь → оплата → (необязательно) результат. Возвращает заказ."""
    token = await login(client, f"buyer-{uuid4().hex[:12]}@example.com")
    reservation = (
        await client.post(f"/api/sales/{sale_id}/reservations", headers=auth(token))
    ).json()
    order = (
        await client.post(f"/api/reservations/{reservation['id']}/pay", headers=auth(token))
    ).json()

    if status is not None:
        body = json.dumps(
            {
                "payment_id": "stub-shop",
                "idempotency_key": idempotency_key_for(UUID(reservation["id"])),
                "status": status,
            },
            separators=(",", ":"),
        ).encode()
        response = await client.post(
            "/api/payments/webhook", content=body, headers=webhook_headers(body)
        )
        assert response.status_code == 200, response.text
    return order


async def shop_sale(client: AsyncClient, token: str, sale_id: UUID) -> dict | None:
    """Своя распродажа из выдачи дашборда, найденная по id."""
    response = await client.get("/api/shop/sales", headers=auth(token))
    assert response.status_code == 200, response.text
    return next((row for row in response.json()["sales"] if row["id"] == str(sale_id)), None)


async def test_buyer_gets_403(client: AsyncClient) -> None:
    buyer_token = await login(client, f"buyer-{uuid4().hex[:12]}@example.com")

    response = await client.get("/api/shop/sales", headers=auth(buyer_token))

    assert response.status_code == 403
    assert "магазин" in response.json()["detail"].lower()


async def test_anonymous_gets_401(client: AsyncClient) -> None:
    assert (await client.get("/api/shop/sales")).status_code == 401


async def test_shop_sees_only_own_sales(client: AsyncClient, session: AsyncSession) -> None:
    mine_id, mine_token = await make_shop(session, client)
    other_id, other_token = await make_shop(session, client)
    my_sale = await make_sale_for(session, mine_id)
    other_sale = await make_sale_for(session, other_id)

    mine = (await client.get("/api/shop/sales", headers=auth(mine_token))).json()
    ids = {row["id"] for row in mine["sales"]}

    assert str(my_sale) in ids
    assert str(other_sale) not in ids, "видна чужая распродажа"

    # И наоборот.
    theirs = (await client.get("/api/shop/sales", headers=auth(other_token))).json()
    their_ids = {row["id"] for row in theirs["sales"]}
    assert str(other_sale) in their_ids
    assert str(my_sale) not in their_ids
    assert mine["server_time"]


async def test_revenue_counts_only_paid_orders(
    client: AsyncClient,
    session: AsyncSession,
    payment_provider: FakePaymentProvider,
) -> None:
    """Выручка — только `paid`. pending_payment и failed не считаются."""
    shop_id, token = await make_shop(session, client)
    sale_id = await make_sale_for(session, shop_id, quantity=5, price_minor=250_000)

    await buy_and_settle(client, sale_id, "succeeded")
    await buy_and_settle(client, sale_id, "succeeded")
    await buy_and_settle(client, sale_id, "declined")
    # Третий покупатель начал оплату и не закончил.
    await buy_and_settle(client, sale_id, None)

    row = await shop_sale(client, token, sale_id)

    assert row is not None
    assert row["paid_orders_count"] == 2, "в счёт заказов попало лишнее"
    assert row["revenue_minor"] == 500_000, "выручка посчитана не только по paid"
    assert row["sold"] == 2
    # Незавершённая оплата всё ещё держит единицу.
    assert row["held"] == 1
    assert row["remaining"] == 2
    await assert_counters_consistent(session, sale_id)


async def test_revenue_is_zero_without_paid_orders(
    client: AsyncClient,
    session: AsyncSession,
    payment_provider: FakePaymentProvider,
) -> None:
    shop_id, token = await make_shop(session, client)
    sale_id = await make_sale_for(session, shop_id)
    await buy_and_settle(client, sale_id, "declined")

    row = await shop_sale(client, token, sale_id)

    assert row is not None
    assert (row["revenue_minor"], row["paid_orders_count"]) == (0, 0)


async def test_unsold_appears_only_after_closing(
    client: AsyncClient,
    session: AsyncSession,
    payment_provider: FakePaymentProvider,
) -> None:
    shop_id, token = await make_shop(session, client)
    sale_id = await make_sale_for(session, shop_id, quantity=5)
    await buy_and_settle(client, sale_id, "succeeded")

    running = await shop_sale(client, token, sale_id)
    assert running is not None
    assert running["unsold"] is None, "пока распродажа идёт, «не продано» не считается"
    assert running["status"] == "active"
    assert running["closed_at"] is None

    # Закрываем распродажу.
    await session.execute(
        text("update sales set end_at = now() - interval '1 second' where id = :id"),
        {"id": sale_id},
    )
    await session.commit()
    await close_sales_once(session)

    closed = await shop_sale(client, token, sale_id)
    assert closed is not None
    assert closed["status"] == "ended"
    assert closed["closed_at"] is not None
    assert closed["unsold"] == 4, "не продано = quantity - sold"
    assert closed["quantity"] == 5, "quantity не должен меняться"
    assert closed["sold"] == 1
    await assert_counters_consistent(session, sale_id)


async def test_counters_match_consistency_check(
    client: AsyncClient,
    session: AsyncSession,
    payment_provider: FakePaymentProvider,
) -> None:
    """Цифры дашборда совпадают с тем, что проверяет assert_counters_consistent."""
    shop_id, token = await make_shop(session, client)
    sale_id = await make_sale_for(session, shop_id, quantity=5)
    await buy_and_settle(client, sale_id, "succeeded")
    await buy_and_settle(client, sale_id, None)

    row = await shop_sale(client, token, sale_id)
    assert row is not None

    await session.rollback()
    counters = (
        await session.execute(
            text("""
                select
                    s.quantity, s.sold, s.held,
                    (select count(*) from reservations r
                     where r.sale_id = s.id
                       and r.status in ('held', 'paying', 'paid')) as active
                from sales s where s.id = :id
            """),
            {"id": sale_id},
        )
    ).one()

    assert (row["quantity"], row["sold"], row["held"]) == (
        counters.quantity,
        counters.sold,
        counters.held,
    )
    assert row["remaining"] == counters.quantity - counters.sold - counters.held
    assert counters.sold + counters.held == counters.active
    await assert_counters_consistent(session, sale_id)


async def test_dashboard_uses_single_query_without_n_plus_one(
    client: AsyncClient,
    session: AsyncSession,
    payment_provider: FakePaymentProvider,
) -> None:
    """Агрегаты считаются одним запросом, а не по запросу на распродажу."""
    from sqlalchemy import event

    from app.core.db import get_engine

    shop_id, token = await make_shop(session, client)
    for _ in range(4):
        sale_id = await make_sale_for(session, shop_id)
        await buy_and_settle(client, sale_id, "succeeded")

    statements: list[str] = []

    def record(_conn, _cursor, statement, *_args) -> None:
        statements.append(statement)

    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        response = await client.get("/api/shop/sales", headers=auth(token))
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert response.status_code == 200
    assert len(response.json()["sales"]) >= 4
    # select now() для server_time плюс сам запрос распродаж. Рост числа
    # распродаж не должен добавлять запросов.
    selects = [s for s in statements if "from sales" in s.lower()]
    assert len(selects) == 1, f"запросов к sales: {len(selects)}\n{selects}"


async def test_shop_with_no_sales_gets_empty_list(
    client: AsyncClient, session: AsyncSession
) -> None:
    _, token = await make_shop(session, client)

    body = (await client.get("/api/shop/sales", headers=auth(token))).json()

    assert body["sales"] == []
    assert body["server_time"], "server_time нужен и для пустого списка"


async def test_sale_fields_are_complete(client: AsyncClient, session: AsyncSession) -> None:
    shop_id, token = await make_shop(session, client)
    sale_id = await make_sale_for(session, shop_id, quantity=7, price_minor=123_400)

    row = await shop_sale(client, token, sale_id)

    assert row is not None
    assert row["price_minor"] == 123_400
    assert row["currency"] == "KGS"
    assert row["quantity"] == 7
    assert row["product_name"].startswith("Товар ")
    assert row["product_id"]
    assert row["start_at"] and row["end_at"]
