"""Одновременное «Купить» от нескольких покупателей — для ручной проверки.

    docker compose exec backend python -m app.scripts.race
    docker compose exec backend python -m app.scripts.race buyer1@example.com buyer2@example.com

Берёт первую активную распродажу и бьёт по ней запросами из `asyncio.gather`:
это настоящая параллельность, а не нажатия подряд. Удобно, когда нужно увидеть
гонку за последнюю единицу в браузере, не успевая нажать в двух вкладках.

Нагрузочный скрипт дня 4 — отдельная история: здесь нет ни замеров, ни
нарастающей нагрузки, только один залп.
"""

import asyncio
import sys

import httpx

DEFAULT_EMAILS = ("buyer1@example.com", "buyer2@example.com")
BASE_URL = "http://localhost:8000"


async def login(client: httpx.AsyncClient, email: str) -> str:
    response = await client.post("/api/auth/login", json={"email": email})
    response.raise_for_status()
    return response.json()["access_token"]


async def buy(client: httpx.AsyncClient, sale_id: str, email: str, token: str) -> None:
    response = await client.post(
        f"/api/sales/{sale_id}/reservations",
        headers={"Authorization": f"Bearer {token}"},
    )
    if response.status_code == 201:
        print(f"  {email:24} 201  бронь {response.json()['id']}")
        return

    detail = response.json().get("detail")
    reason = detail.get("code") if isinstance(detail, dict) else detail
    hint = ""
    if reason == "already_reserved":
        # Не гонка, а прошлый прогон: бронь этого покупателя всё ещё жива.
        hint = "  ← бронь с прошлого раза; отмените её или пересоздайте распродажу"
    print(f"  {email:24} {response.status_code}  {reason}{hint}")


async def run(emails: list[str]) -> None:
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        sales = (await client.get("/api/sales")).json()
        active = [sale for sale in sales if sale["status"] == "active"]
        if not active:
            print("Нет идущей распродажи. Создайте её:")
            print("  python -m app.scripts.seed --start-in 0 --duration 90 --quantity 1")
            return

        # Самая свежая активная, а не первая попавшаяся: скрипт запускают
        # сразу после seed, и ожидают гонку именно по только что созданной
        # распродаже. Первая в списке может оказаться прошлой, где у этих
        # покупателей уже есть брони, — и вместо гонки видно already_reserved.
        sale = max(active, key=lambda item: item["start_at"])
        print(
            f"распродажа {sale['id']}: quantity={sale['quantity']}, remaining={sale['remaining']}\n"
        )

        if sale["remaining"] > 1:
            print(
                f"  ! в распродаже свободно {sale['remaining']} единиц — "
                "гонки за последнюю не будет, оба покупателя получат бронь.\n"
                "    Чтобы увидеть гонку, пересоздайте распродажу на одну единицу:\n"
                "      python -m app.scripts.seed --start-in 0 --duration 90 --quantity 1\n"
            )

        tokens = [await login(client, email) for email in emails]

        # gather, а не последовательные вызовы: запросы уходят одновременно.
        await asyncio.gather(
            *(
                buy(client, sale["id"], email, token)
                for email, token in zip(emails, tokens, strict=True)
            )
        )

        after = (await client.get(f"/api/sales/{sale['id']}")).json()
        print(f"\nостаток после гонки: {after['remaining']}")


def main() -> None:
    emails = sys.argv[1:] or list(DEFAULT_EMAILS)
    asyncio.run(run(emails))


if __name__ == "__main__":
    main()
