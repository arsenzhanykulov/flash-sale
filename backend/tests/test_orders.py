"""Заказы покупателя: GET /api/orders/me."""

import json
from uuid import UUID, uuid4

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.payments.flow import idempotency_key_for
from tests.conftest import FakePaymentProvider, MakeSale, webhook_headers


async def login(client: AsyncClient) -> str:
    response = await client.post(
        "/api/auth/login", json={"email": f"orders-{uuid4().hex[:12]}@example.com"}
    )
    return response.json()["access_token"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def place_order(client: AsyncClient, sale_id, token: str) -> dict:
    reservation = (
        await client.post(f"/api/sales/{sale_id}/reservations", headers=auth(token))
    ).json()
    order = (
        await client.post(f"/api/reservations/{reservation['id']}/pay", headers=auth(token))
    ).json()
    return {"reservation_id": reservation["id"], **order}


async def settle(client: AsyncClient, reservation_id: str, status: str) -> None:
    body = json.dumps(
        {
            "payment_id": "stub-orders",
            "idempotency_key": idempotency_key_for(UUID(reservation_id)),
            "status": status,
        },
        separators=(",", ":"),
    ).encode()
    response = await client.post(
        "/api/payments/webhook", content=body, headers=webhook_headers(body)
    )
    assert response.status_code == 200, response.text


async def test_my_orders_returns_only_own(
    client: AsyncClient, make_sale: MakeSale, payment_provider: FakePaymentProvider
) -> None:
    sale_id = await make_sale(quantity=5)
    mine_token = await login(client)
    stranger_token = await login(client)
    mine = await place_order(client, sale_id, mine_token)
    stranger = await place_order(client, sale_id, stranger_token)

    response = await client.get("/api/orders/me", headers=auth(mine_token))

    assert response.status_code == 200
    body = response.json()
    ids = [order["id"] for order in body["orders"]]
    assert ids == [mine["order_id"]]
    assert stranger["order_id"] not in ids
    assert body["server_time"]


async def test_my_orders_are_sorted_newest_first(
    client: AsyncClient, make_sale: MakeSale, payment_provider: FakePaymentProvider
) -> None:
    token = await login(client)
    first = await place_order(client, await make_sale(quantity=3), token)
    second = await place_order(client, await make_sale(quantity=3), token)
    third = await place_order(client, await make_sale(quantity=3), token)

    body = (await client.get("/api/orders/me", headers=auth(token))).json()

    ids = [order["id"] for order in body["orders"]]
    assert ids == [third["order_id"], second["order_id"], first["order_id"]]


async def test_my_orders_requires_login(client: AsyncClient) -> None:
    assert (await client.get("/api/orders/me")).status_code == 401


async def test_order_fields_reflect_payment_result(
    client: AsyncClient,
    session: AsyncSession,
    make_sale: MakeSale,
    payment_provider: FakePaymentProvider,
) -> None:
    sale_id = await make_sale(quantity=5)
    token = await login(client)
    order = await place_order(client, sale_id, token)

    pending = (await client.get("/api/orders/me", headers=auth(token))).json()["orders"][0]
    assert pending["status"] == "pending_payment"
    assert pending["paid_at"] is None
    assert pending["amount_minor"] == 100000
    assert pending["currency"] == "KGS"
    assert pending["product_name"].startswith("Товар ")
    assert pending["sale_id"] == str(sale_id)

    await settle(client, order["reservation_id"], "succeeded")

    paid = (await client.get("/api/orders/me", headers=auth(token))).json()["orders"][0]
    assert paid["status"] == "paid"
    assert paid["paid_at"] is not None


async def test_declined_order_is_visible_as_failed(
    client: AsyncClient, make_sale: MakeSale, payment_provider: FakePaymentProvider
) -> None:
    sale_id = await make_sale(quantity=5)
    token = await login(client)
    order = await place_order(client, sale_id, token)

    await settle(client, order["reservation_id"], "declined")

    failed = (await client.get("/api/orders/me", headers=auth(token))).json()["orders"][0]
    assert failed["status"] == "failed"
    assert failed["paid_at"] is None


async def test_buyer_without_orders_gets_empty_list(client: AsyncClient) -> None:
    token = await login(client)

    body = (await client.get("/api/orders/me", headers=auth(token))).json()

    assert body["orders"] == []
    assert body["server_time"], "server_time нужен и для пустого списка"
