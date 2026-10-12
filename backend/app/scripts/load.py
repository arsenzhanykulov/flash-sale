"""Нагрузочный прогон: N покупателей одновременно на одну распродажу.

    docker compose exec backend python -m app.scripts.load
    docker compose exec backend python -m app.scripts.load --buyers 500 --quantity 20

Что проверяется: при любом числе одновременных покупателей продано ровно
столько, сколько было в наличии, и ни единицей больше. Отказы — честные
(`sold_out`), а не ошибки сервера.

Покупательский путь идёт **по HTTP в запущенный backend**, а не вызовами
сервисов напрямую: проверяется весь путь целиком — пул соединений, резерв,
оплата, вебхук заглушки и запись письма в outbox. Прямо в БД скрипт только
создаёт распродажу (API для этого нет) и читает итоговый отчёт.

Скрипт создаёт **свою** распродажу со своим товаром и своими покупателями
и считает только по ней: в базе лежат демо-данные и следы ручных проверок,
и отчёт по всей таблице не значил бы ничего (правило из CLAUDE.md).

Код выхода ненулевой, если нарушен хоть один из итогов: оверсейл, остались
брони в `held` или писем не ровно столько же, сколько продано. Скрипт должен
быть пригоден для CI, а не только для чтения глазами.

От `race.py` отличается масштабом и замерами: там один залп двух покупателей
для наглядной демонстрации в браузере, здесь — нагрузка с отчётом.
"""

import argparse
import asyncio
import time
from collections import Counter
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_sessionmaker, reset_engine

BASE_URL = "http://localhost:8000"

DEFAULT_BUYERS = 200
DEFAULT_QUANTITY = 10
# Сколько ждать, пока оплаты досчитаются: вебхук заглушки, а при его потере —
# сверка. Потолок сверки по умолчанию 300 с, но в штатном прогоне вебхук
# приходит сразу, поэтому ждать так долго незачем.
DEFAULT_SETTLE_TIMEOUT = 60.0

SHOP_EMAIL = "load@example.com"


@dataclass
class Outcome:
    """Что ответил сервер на залп запросов."""

    reserved: list[tuple[str, str]] = field(default_factory=list)  # (token, reservation_id)
    refusals: Counter[str] = field(default_factory=Counter)
    errors: Counter[str] = field(default_factory=Counter)


async def create_sale(session: AsyncSession, *, quantity: int, run_id: str) -> UUID:
    """Своя распродажа: свой магазин, свой товар с уникальным именем."""
    shop_id = await session.scalar(
        text("""
            insert into users (email, role) values (:email, 'shop')
            on conflict (email) do update set role = 'shop'
            returning id
        """),
        {"email": SHOP_EMAIL},
    )
    product_id = await session.scalar(
        text("insert into products (shop_id, name) values (:shop_id, :name) returning id"),
        {"shop_id": shop_id, "name": f"Нагрузочный прогон {run_id}"},
    )
    sale_id = await session.scalar(
        text("""
            insert into sales (product_id, price_minor, currency, quantity, start_at, end_at)
            values (:product_id, 499000, 'KGS', :quantity,
                    now() - interval '1 second', now() + interval '30 minutes')
            returning id
        """),
        {"product_id": product_id, "quantity": quantity},
    )
    await session.commit()
    return sale_id


async def login(client: httpx.AsyncClient, email: str) -> str | None:
    try:
        response = await client.post("/api/auth/login", json={"email": email})
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    return response.json()["access_token"]


async def reserve(client: httpx.AsyncClient, sale_id: UUID, token: str, out: Outcome) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = await client.post(f"/api/sales/{sale_id}/reservations", headers=headers)
    except httpx.HTTPError as error:
        out.errors[type(error).__name__] += 1
        return

    if response.status_code == 201:
        out.reserved.append((token, response.json()["id"]))
        return

    # Отказ резерва приходит объектом с машинным кодом (см. README).
    detail = response.json().get("detail")
    code = detail.get("code") if isinstance(detail, dict) else str(detail)
    out.refusals[f"{response.status_code} {code}"] += 1


async def pay(
    client: httpx.AsyncClient, reservation_id: str, token: str, results: Counter[str]
) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = await client.post(f"/api/reservations/{reservation_id}/pay", headers=headers)
    except httpx.HTTPError as error:
        results[type(error).__name__] += 1
        return
    if response.status_code in (200, 201, 202):
        body = response.json()
        results[f"{response.status_code} {body.get('order_status', '?')}"] += 1
        return
    detail = response.json().get("detail")
    code = detail.get("code") if isinstance(detail, dict) else str(detail)
    results[f"{response.status_code} {code}"] += 1


async def set_stub_mode(mode: str) -> str | None:
    """Переключить заглушку в нужный режим, вернув прежний (для восстановления)."""
    stub = get_settings().payment_stub_url
    async with httpx.AsyncClient(base_url=stub, timeout=5.0) as client:
        try:
            previous = (await client.get("/control/mode")).json()["mode"]
            await client.post("/control/mode", json={"mode": mode})
        except httpx.HTTPError as error:
            print(f"  ! заглушка недоступна ({error}): оплаты не досчитаются")
            return None
    return previous


async def wait_until_settled(session: AsyncSession, sale_id: UUID, timeout: float) -> bool:
    """Дождаться, пока по нашей распродаже не останется броней в paying."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await session.rollback()  # свежий снимок: иначе видим начало транзакции
        paying = await session.scalar(
            text("""
                select count(*) from reservations
                where sale_id = :sale_id and status = 'paying'
            """),
            {"sale_id": sale_id},
        )
        if paying == 0:
            return True
        await asyncio.sleep(0.5)
    return False


async def report(session: AsyncSession, sale_id: UUID, *, outcome: Outcome, elapsed: float) -> int:
    """Отчёт по своей распродаже. Возвращает код выхода: 0 — инварианты целы."""
    await session.rollback()
    sale = (
        await session.execute(
            text("""
                select quantity, sold, held, quantity - sold - held as remaining
                from sales where id = :id
            """),
            {"id": sale_id},
        )
    ).one()

    reservations = dict(
        (
            await session.execute(
                text("""
                    select status, count(*) from reservations
                    where sale_id = :id group by status order by status
                """),
                {"id": sale_id},
            )
        ).all()  # type: ignore[arg-type]
    )
    orders = dict(
        (
            await session.execute(
                text("""
                    select status, count(*) from orders
                    where sale_id = :id group by status order by status
                """),
                {"id": sale_id},
            )
        ).all()  # type: ignore[arg-type]
    )
    # Письма ищем по sale_id внутри payload: outbox общий, а нас интересуют
    # только письма этого прогона.
    letters = (
        await session.execute(
            text("""
                select kind, status, count(*) as count from outbox
                where payload->>'sale_id' = :id
                group by kind, status order by kind, status
            """),
            {"id": str(sale_id)},
        )
    ).all()

    paid_orders = orders.get("paid", 0)
    letters_total = sum(row.count for row in letters)
    oversold = sale.sold + sale.held > sale.quantity or sale.sold > sale.quantity

    print(f"\n=== распродажа {sale_id} ===")
    print(f"  продано .............. {sale.sold} из {sale.quantity}")
    print(f"  в корзинах (held) .... {sale.held}")
    print(f"  осталось ............. {sale.remaining}")
    print(
        "  оверсейл ............. "
        + (
            "ДА — ИНВАРИАНТ НАРУШЕН"
            if oversold
            else f"нет ({sale.sold} + {sale.held} ≤ {sale.quantity})"
        )
    )
    print(f"  брони по статусам .... {reservations}")
    print(f"  заказы по статусам ... {orders}")
    print(f"  писем в outbox ....... {letters_total}")
    for row in letters:
        print(f"      {row.kind} / {row.status}: {row.count}")

    print("\n  отказы по кодам:")
    for code, count in outcome.refusals.most_common():
        print(f"      {code:28} {count}")
    if outcome.errors:
        print(f"  ошибки транспорта .... {dict(outcome.errors)}")
    print(f"\n  время прогона ........ {elapsed:.2f} с")

    # Каждый пункт — отдельный итог, чтобы в выводе было видно, что именно
    # разошлось, а не просто «прогон неудачный».
    problems: list[str] = []
    if oversold:
        print(f"\nОВЕРСЕЙЛ: продано {sale.sold} при quantity {sale.quantity}.")
        problems.append("оверсейл")
    if sale.held != 0:
        print(
            f"\nОСТАЛИСЬ БРОНИ В HELD: {sale.held}. Все победители залпа должны были "
            "оплатить; незакрытая бронь — либо упавшая оплата, либо недождавшийся досчёт."
        )
        problems.append("held != 0")
    granted = len(outcome.reserved)
    if granted != sale.sold + sale.held:
        print(
            f"\nБРОНИ РАЗОШЛИСЬ С БАЗОЙ: сервер ответил 201 на {granted} запросов, "
            f"а продано + held = {sale.sold + sale.held}. В режиме approve каждая "
            "выданная бронь обязана стать продажей."
        )
        problems.append("выданные брони != продано + held")
    if letters_total != sale.sold:
        print(
            f"\nПИСЬМА НЕ СОШЛИСЬ: писем {letters_total}, продано {sale.sold} "
            f"(оплаченных заказов {paid_orders}). Инвариант 6 — ровно одно письмо "
            "на событие."
        )
        problems.append("писем != продано")

    if problems:
        print(f"\nПрогон неудачный: {', '.join(problems)}.")
        return 1
    print(
        f"\nИнварианты целы: продано ровно {sale.sold} из {sale.quantity}, "
        f"броней в held нет, писем ровно {letters_total}."
    )
    return 0


async def run(buyers: int, quantity: int, settle_timeout: float) -> int:
    run_id = uuid4().hex[:8]
    settings = get_settings()
    print(f"прогон {run_id}: покупателей {buyers}, в наличии {quantity}")
    print(f"пул соединений: {settings.db_pool_size} + {settings.db_max_overflow} overflow")

    # Режим выставляется явно: прогон не должен зависеть от того, в каком
    # состоянии заглушку оставила прошлая ручная проверка.
    previous_mode = await set_stub_mode("approve")
    print(f"заглушка: approve (прежний режим {previous_mode or 'неизвестен'})")

    try:
        return await measure(buyers, quantity, settle_timeout, run_id=run_id)
    finally:
        # Возвращаем режим и при падении: иначе прогон молча менял бы
        # окружение для следующей ручной проверки.
        if previous_mode is not None and previous_mode != "approve":
            await set_stub_mode(previous_mode)
            print(f"режим заглушки возвращён в {previous_mode}")


async def measure(buyers: int, quantity: int, settle_timeout: float, *, run_id: str) -> int:
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        sale_id = await create_sale(session, quantity=quantity, run_id=run_id)

        # max_connections по числу покупателей: с дефолтом httpx (100) запросы
        # уходили бы пачками, и залпа не получилось бы.
        #
        # keepalive_expiry меньше, чем timeout_keep_alive у uvicorn (5 с):
        # иначе сервер закрывает простоявшее соединение, клиент в тот же
        # момент его переиспользует и получает RemoteProtocolError. В первом
        # прогоне так отвалились 4 запроса из 200 — гонка на стороне клиента,
        # в логах backend при этом не было ни одной ошибки.
        limits = httpx.Limits(
            max_connections=buyers + 10,
            max_keepalive_connections=buyers + 10,
            keepalive_expiry=2.0,
        )
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=60.0, limits=limits) as client:
            print("логин покупателей…")
            tokens = await asyncio.gather(
                *(login(client, f"load-{run_id}-{i}@example.com") for i in range(buyers))
            )
            alive = [token for token in tokens if token is not None]
            if len(alive) != buyers:
                print(f"  ! вошли {len(alive)} из {buyers}")

            outcome = Outcome()
            print(f"залп: {len(alive)} одновременных «Купить»…")
            started = time.perf_counter()
            await asyncio.gather(*(reserve(client, sale_id, token, outcome) for token in alive))
            reserve_elapsed = time.perf_counter() - started
            print(f"  брони получили {len(outcome.reserved)} за {reserve_elapsed:.2f} с")

            print(f"оплата: {len(outcome.reserved)} победителей…")
            pay_results: Counter[str] = Counter()
            await asyncio.gather(
                *(
                    pay(client, reservation_id, token, pay_results)
                    for token, reservation_id in outcome.reserved
                )
            )
            print(f"  ответы оплаты: {dict(pay_results)}")

        print("ждём досчёта оплат…")
        if not await wait_until_settled(session, sale_id, settle_timeout):
            print(f"  ! за {settle_timeout:.0f} с остались брони в paying — смотрите отчёт")
        elapsed = time.perf_counter() - started

        return await report(session, sale_id, outcome=outcome, elapsed=elapsed)


def main() -> None:
    parser = argparse.ArgumentParser(description="Нагрузочный прогон по одной распродаже")
    parser.add_argument(
        "--buyers",
        type=int,
        default=DEFAULT_BUYERS,
        help=f"сколько покупателей жмут «Купить» одновременно (по умолчанию {DEFAULT_BUYERS})",
    )
    parser.add_argument(
        "--quantity",
        type=int,
        default=DEFAULT_QUANTITY,
        help=f"сколько единиц в распродаже (по умолчанию {DEFAULT_QUANTITY})",
    )
    parser.add_argument(
        "--settle-timeout",
        type=float,
        default=DEFAULT_SETTLE_TIMEOUT,
        help="сколько секунд ждать досчёта оплат",
    )
    args = parser.parse_args()
    if args.buyers < 1:
        parser.error("--buyers должен быть не меньше 1")
    if args.quantity < 1:
        parser.error("--quantity должен быть не меньше 1")

    async def entrypoint() -> int:
        try:
            return await run(args.buyers, args.quantity, args.settle_timeout)
        finally:
            await reset_engine()

    raise SystemExit(asyncio.run(entrypoint()))


if __name__ == "__main__":
    main()
