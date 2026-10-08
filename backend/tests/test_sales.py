"""Чтение распродаж: статус по времени БД, остаток, 404."""

from uuid import uuid4

import pytest
from httpx import AsyncClient

from tests.conftest import MakeSale


@pytest.mark.parametrize(
    ("start_at", "end_at", "closed", "expected_status"),
    [
        # Далеко до старта.
        ("now() + interval '1 minute'", "now() + interval '2 minutes'", False, "upcoming"),
        # Граница: за секунду до старта ещё upcoming.
        ("now() + interval '1 second'", "now() + interval '1 hour'", False, "upcoming"),
        # Момент старта: now() >= start_at — уже active.
        ("now()", "now() + interval '1 hour'", False, "active"),
        # Идёт.
        ("now() - interval '1 minute'", "now() + interval '1 hour'", False, "active"),
        # Граница: секунду назад закончилась.
        ("now() - interval '1 hour'", "now() - interval '1 second'", False, "ended"),
        # Закончилась давно.
        ("now() - interval '2 hours'", "now() - interval '1 hour'", False, "ended"),
        # Закрыта воркером досрочно — тоже ended, хотя окно ещё не истекло.
        ("now() - interval '1 minute'", "now() + interval '1 hour'", True, "ended"),
    ],
)
async def test_sale_status_on_time_boundaries(
    client: AsyncClient,
    make_sale: MakeSale,
    start_at: str,
    end_at: str,
    closed: bool,
    expected_status: str,
) -> None:
    sale_id = await make_sale(start_at=start_at, end_at=end_at, closed=closed)

    response = await client.get(f"/api/sales/{sale_id}")

    assert response.status_code == 200
    assert response.json()["status"] == expected_status


async def test_remaining_excludes_sold_and_held(client: AsyncClient, make_sale: MakeSale) -> None:
    sale_id = await make_sale(quantity=5, sold=2, held=1)

    body = (await client.get(f"/api/sales/{sale_id}")).json()

    assert body["quantity"] == 5
    assert body["remaining"] == 2


async def test_remaining_is_zero_when_sold_out(client: AsyncClient, make_sale: MakeSale) -> None:
    sale_id = await make_sale(quantity=3, sold=2, held=1)

    body = (await client.get(f"/api/sales/{sale_id}")).json()

    assert body["remaining"] == 0


async def test_sale_detail_returns_product_and_server_time(
    client: AsyncClient, make_sale: MakeSale
) -> None:
    sale_id = await make_sale()

    body = (await client.get(f"/api/sales/{sale_id}")).json()

    assert body["id"] == str(sale_id)
    assert body["product_name"].startswith("Товар ")
    assert body["price_minor"] == 100000
    assert body["currency"] == "KGS"
    # Статус посчитан на server_time — фронт считает таймер по нему.
    assert body["server_time"]


async def test_sales_list_contains_created_sale(client: AsyncClient, make_sale: MakeSale) -> None:
    sale_id = await make_sale()

    response = await client.get("/api/sales")

    assert response.status_code == 200
    assert str(sale_id) in [sale["id"] for sale in response.json()]


async def test_unknown_sale_is_404(client: AsyncClient) -> None:
    response = await client.get(f"/api/sales/{uuid4()}")

    assert response.status_code == 404
    assert response.json()["detail"] == "Распродажа не найдена"


async def test_malformed_sale_id_is_422(client: AsyncClient) -> None:
    response = await client.get("/api/sales/not-a-uuid")
    assert response.status_code == 422
