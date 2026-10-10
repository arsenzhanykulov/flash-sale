"""Live-обновления остатка по WebSocket.

Вход не нужен: смотреть витрину может любой.
"""

import asyncio
import logging
from contextlib import suppress
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.api.schemas.live import SaleSnapshotMessage
from app.core.db import get_sessionmaker
from app.services.live_updates import Subscriber, hub
from app.services.sales import SaleSnapshot, get_sale_snapshot

logger = logging.getLogger(__name__)

router = APIRouter()

# Своя причина закрытия: 1000–2999 заняты протоколом, приложению отданы 4000+.
SALE_NOT_FOUND_CODE = 4404


def to_message(snapshot: SaleSnapshot) -> dict:
    return SaleSnapshotMessage.model_validate(snapshot).model_dump(mode="json")


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


async def pump_until_disconnect(websocket: WebSocket, queue: Subscriber) -> None:
    """Пересылать снимки, пока клиент на связи.

    Входящие сообщения тоже читаем: иначе отключение клиента осталось бы
    незамеченным, пока мы ждём очередь, и подписка висела бы зря.
    """
    forward = asyncio.create_task(forward_snapshots(websocket, queue))
    watch = asyncio.create_task(watch_disconnect(websocket))
    done, pending = await asyncio.wait((forward, watch), return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    for task in done:
        with suppress(WebSocketDisconnect, RuntimeError):
            task.result()


async def forward_snapshots(websocket: WebSocket, queue: Subscriber) -> None:
    while True:
        snapshot = await queue.get()
        await websocket.send_json(to_message(snapshot))


async def watch_disconnect(websocket: WebSocket) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
