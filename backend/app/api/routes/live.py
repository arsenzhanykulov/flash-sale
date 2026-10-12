"""Live-обновления остатка по WebSocket.

Вход не нужен: смотреть витрину может любой.
"""

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.api.schemas.live import SaleSnapshotMessage
from app.core.db import get_sessionmaker
from app.services.live_updates import Subscriber, hub
from app.services.sales import SaleSnapshot, get_sale_snapshot

logger = logging.getLogger(__name__)

router = APIRouter()

# Свои причины закрытия: 1000–2999 заняты протоколом, приложению отданы 4000+.
SALE_NOT_FOUND_CODE = 4404
TOO_MANY_SALES_CODE = 4413

# Предел на одну подписку: иначе запросом можно заставить сервер читать
# тысячи строк на каждое обновление.
MAX_SUBSCRIBED_SALES = 100


def to_message(snapshot: SaleSnapshot) -> dict:
    return SaleSnapshotMessage.model_validate(snapshot).model_dump(mode="json")


def snapshot_frame(snapshots: list[SaleSnapshot]) -> dict:
    """Первое сообщение: состояние всех запрошенных распродаж сразу.

    Экран отрисовывается целиком, не собираясь из отдельных кадров.
    """
    return {"type": "snapshot", "sales": [to_message(item) for item in snapshots]}


def update_frame(snapshot: SaleSnapshot) -> dict:
    """Обновление — всегда по одной распродаже."""
    return {"type": "update", "sale": to_message(snapshot)}


async def read_snapshots(sale_ids: list[UUID]) -> list[SaleSnapshot]:
    """Снимки нескольких распродаж. Неизвестные id просто отсутствуют в ответе."""
    async with get_sessionmaker()() as session:
        snapshots = [
            snapshot
            for sale_id in sale_ids
            if (snapshot := await get_sale_snapshot(session, sale_id)) is not None
        ]
        await session.rollback()
    return snapshots


async def read_snapshot(sale_id: UUID) -> SaleSnapshot | None:
    """Сессия открывается на один запрос и сразу закрывается.

    Держать её на всё время соединения нельзя: соединение из пула было бы
    занято, пока клиент смотрит страницу, и пятнадцать зрителей исчерпали бы
    пул целиком.
    """
    async with get_sessionmaker()() as session:
        snapshot = await get_sale_snapshot(session, sale_id)
        await session.rollback()
    return snapshot


@router.websocket("/sales/{sale_id}")
async def sale_updates(websocket: WebSocket, sale_id: UUID) -> None:
    await websocket.accept()

    snapshot = await read_snapshot(sale_id)
    if snapshot is None:
        await websocket.close(code=SALE_NOT_FOUND_CODE, reason="Распродажа не найдена")
        return

    async with hub.subscribe(sale_id) as queue:
        # Снимок сразу при подключении: клиент не ждёт первого изменения.
        await websocket.send_json(to_message(snapshot))
        await pump_until_disconnect(websocket, queue)


async def pump_until_disconnect(
    websocket: WebSocket,
    queue: Subscriber,
    *,
    frame: Callable[[SaleSnapshot], dict] = to_message,
) -> None:
    """Пересылать снимки, пока клиент на связи.

    Входящие сообщения тоже читаем: иначе отключение клиента осталось бы
    незамеченным, пока мы ждём очередь, и подписка висела бы зря.
    """
    forward = asyncio.create_task(forward_snapshots(websocket, queue, frame))
    watch = asyncio.create_task(watch_disconnect(websocket))
    done, pending = await asyncio.wait((forward, watch), return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    for task in done:
        with suppress(WebSocketDisconnect, RuntimeError):
            task.result()


async def forward_snapshots(
    websocket: WebSocket, queue: Subscriber, frame: Callable[[SaleSnapshot], dict]
) -> None:
    while True:
        snapshot = await queue.get()
        await websocket.send_json(frame(snapshot))


async def watch_disconnect(websocket: WebSocket) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return


@router.websocket("/sales")
async def many_sale_updates(
    websocket: WebSocket,
    ids: Annotated[str, Query(description="Идентификаторы распродаж через запятую")] = "",
) -> None:
    """Одно соединение на несколько распродаж — для дашборда магазина.

    Сначала приходит кадр `{"type": "snapshot", "sales": [...]}` со всеми
    запрошенными распродажами, дальше `{"type": "update", "sale": {...}}`
    по мере изменений. Неизвестные id не ломают соединение: их просто
    нет в снимке (ADR-026).
    """
    await websocket.accept()

    sale_ids = parse_sale_ids(ids)
    if len(sale_ids) > MAX_SUBSCRIBED_SALES:
        await websocket.close(
            code=TOO_MANY_SALES_CODE,
            reason=f"не больше {MAX_SUBSCRIBED_SALES} распродаж на соединение",
        )
        return

    async with hub.subscribe(*sale_ids) as queue:
        await websocket.send_json(snapshot_frame(await read_snapshots(sale_ids)))
        await pump_until_disconnect(websocket, queue, frame=update_frame)


def parse_sale_ids(raw: str) -> list[UUID]:
    """Разобрать список id из query. Мусор пропускаем, а не падаем."""
    parsed: list[UUID] = []
    for chunk in raw.split(","):
        candidate = chunk.strip()
        if not candidate:
            continue
        try:
            parsed.append(UUID(candidate))
        except ValueError:
            logger.warning("в подписке неожидаемый id распродажи: %r", candidate)
    # dict.fromkeys сохраняет порядок и убирает дубли.
    return list(dict.fromkeys(parsed))
