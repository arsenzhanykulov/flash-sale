"""Live-обновления остатка: WebSocket + Postgres LISTEN/NOTIFY (ADR-003, ADR-020).

Сигнал отправляет триггер на `sales` — внутри той же транзакции, что меняет
счётчики, поэтому он приходит только при коммите. В сигнале лежит один id:
слушатель перечитывает состояние из БД сам, и при обгоне сигналов остаток
не «прыгает назад».

Пачка изменений одной распродажи склеивается коротким окном: десять броней
за `LIVE_UPDATE_WINDOW_MS` дают один запрос к БД и одну рассылку. Распродажи,
на которые никто не подписан, не читаются вовсе.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager, suppress
from uuid import UUID

import asyncpg

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.services.sales import SaleSnapshot, get_sale_snapshot

logger = logging.getLogger(__name__)

CHANNEL = "sale_changed"

# Пауза перед повторным подключением слушателя.
RECONNECT_DELAY_SECONDS = 1.0

# Как часто проверять живость соединения запросом. Нужен на случай, когда
# соединение умерло тихо и add_termination_listener не сработал.
KEEPALIVE_SECONDS = 20.0

Subscriber = asyncio.Queue[SaleSnapshot]


def _put_latest(queue: Subscriber, snapshot: SaleSnapshot) -> None:
    """Держать в очереди только свежий снимок.

    Снимок самодостаточен, промежуточные состояния никому не нужны. Поэтому
    у медленного клиента содержимое очереди заменяется, а не копится, —
    и он не тормозит рассылку остальным.
    """
    while True:
        try:
            queue.put_nowait(snapshot)
            return
        except asyncio.QueueFull:
            with suppress(asyncio.QueueEmpty):
                queue.get_nowait()


class SaleHub:
    """Подписчики на распродажи и пачка распродаж, ждущих рассылки."""

    def __init__(self) -> None:
        self._subscribers: dict[UUID, set[Subscriber]] = {}
        self._dirty: set[UUID] = set()
        self._wakeup = asyncio.Event()

    @asynccontextmanager
    async def subscribe(self, *sale_ids: UUID) -> AsyncIterator[Subscriber]:
        """Одна очередь на несколько распродаж.

        Дашборд магазина смотрит десятки распродаж сразу: отдельное соединение
        на каждую означало бы столько же сокетов, задач и очередей (ADR-026).
        Хаб и так хранит подписчиков по id, поэтому одна очередь просто
        регистрируется под несколькими ключами.
        """
        queue: Subscriber = asyncio.Queue(maxsize=1)
        unique_ids = set(sale_ids)
        for sale_id in unique_ids:
            self._subscribers.setdefault(sale_id, set()).add(queue)
        try:
            yield queue
        finally:
            for sale_id in unique_ids:
                listeners = self._subscribers.get(sale_id)
                if listeners is not None:
                    listeners.discard(queue)
                    if not listeners:
                        del self._subscribers[sale_id]

    def mark_dirty(self, sale_id: UUID) -> None:
        """Вызывается из колбэка asyncpg — поэтому только синхронная работа."""
        self._dirty.add(sale_id)
        self._wakeup.set()

    def mark_subscribed_dirty(self) -> None:
        """Догнать подписчиков: разослать свежие снимки всем, кого смотрят.

        Нужно после переподключения слушателя — сигналы за время разрыва
        потеряны, и единственный честный способ не оставить клиента
        с устаревшим остатком.
        """
        self._dirty.update(self._subscribers)
        if self._dirty:
            self._wakeup.set()

    def watched(self, sale_ids: Iterable[UUID]) -> list[UUID]:
        return [sale_id for sale_id in sale_ids if sale_id in self._subscribers]

    def publish(self, sale_id: UUID, snapshot: SaleSnapshot) -> None:
        for queue in tuple(self._subscribers.get(sale_id, ())):
            _put_latest(queue, snapshot)

    async def take_batch(self, window_seconds: float) -> set[UUID]:
        """Дождаться сигнала и собрать всё, что прилетело за окно склейки."""
        await self._wakeup.wait()
        self._wakeup.clear()
        if window_seconds > 0:
            await asyncio.sleep(window_seconds)
        batch, self._dirty = self._dirty, set()
        return batch


class SaleChangeListener:
    """Слушатель LISTEN на отдельном соединении вне пула.

    LISTEN занимает соединение на всё время работы, поэтому возвращать его
    в пул нельзя: соединение берётся напрямую через asyncpg.
    """

    def __init__(self, hub: SaleHub, *, window_ms: int | None = None) -> None:
        self._hub = hub
        self._window_ms = window_ms
        # Наблюдаемое состояние: по нему видно, жив ли слушатель и сколько
        # раз он переподключался.
        self.backend_pid: int | None = None
        self.connects = 0

    @property
    def _window_seconds(self) -> float:
        window_ms = (
            self._window_ms if self._window_ms is not None else get_settings().live_update_window_ms
        )
        return window_ms / 1000

    async def run(self) -> None:
        """Бесконечный цикл с переподключением. Остановка — через cancel."""
        while True:
            try:
                await self._listen_until_lost()
            except asyncio.CancelledError:
                logger.info("слушатель live-обновлений остановлен")
                raise
            except Exception:
                logger.exception("слушатель live-обновлений упал, переподключаемся")
            finally:
                self.backend_pid = None
            await asyncio.sleep(RECONNECT_DELAY_SECONDS)

    async def _listen_until_lost(self) -> None:
        connection = await asyncpg.connect(get_settings().asyncpg_dsn)
        lost = asyncio.Event()
        connection.add_termination_listener(lambda _connection: lost.set())
        await connection.add_listener(CHANNEL, self._on_notify)

        self.backend_pid = connection.get_server_pid()
        self.connects += 1
        logger.info(
            "слушатель live-обновлений подключён, backend_pid=%s, подключений=%d",
            self.backend_pid,
            self.connects,
        )

        worker = asyncio.create_task(self._broadcast_worker(), name="live-broadcast")
        # Сигналы за время разрыва потеряны — досылаем свежие снимки.
        self._hub.mark_subscribed_dirty()
        try:
            await self._wait_until_lost(connection, lost)
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
            with suppress(Exception):
                await connection.close(timeout=1)

    async def _wait_until_lost(self, connection: asyncpg.Connection, lost: asyncio.Event) -> None:
        while not lost.is_set():
            with suppress(TimeoutError):
                await asyncio.wait_for(lost.wait(), timeout=KEEPALIVE_SECONDS)
            if lost.is_set():
                return
            # Соединение могло умереть тихо — проверяем запросом.
            await connection.execute("select 1")

    def _on_notify(self, _connection: object, _pid: int, _channel: str, payload: str) -> None:
        try:
            self._hub.mark_dirty(UUID(payload))
        except ValueError:
            logger.warning("в канале %s неожидаемый payload: %r", CHANNEL, payload)

    async def _broadcast_worker(self) -> None:
        while True:
            batch = await self._hub.take_batch(self._window_seconds)
            watched = self._hub.watched(batch)
            if not watched:
                continue

            async with get_sessionmaker()() as session:
                for sale_id in watched:
                    snapshot = await get_sale_snapshot(session, sale_id)
                    if snapshot is not None:
                        self._hub.publish(sale_id, snapshot)
                await session.rollback()


# Один хаб на процесс: к нему обращаются и слушатель, и WebSocket-эндпоинт.
hub = SaleHub()
