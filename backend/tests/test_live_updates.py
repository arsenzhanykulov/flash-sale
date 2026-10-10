"""Live-обновления остатка: триггер → NOTIFY → слушатель → WebSocket.

Слушатель поднимается фикстурой, поэтому тесты проходят весь путь целиком,
а не только хаб: lifespan приложения в тестах не прогоняется.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from httpx_ws import WebSocketDisconnect, aconnect_ws
from httpx_ws.transport import ASGIWebSocketTransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.live import SALE_NOT_FOUND_CODE
from app.services.live_updates import SaleChangeListener, SaleHub
from app.services.reservations import cancel_reservation, create_reservation
from app.services.sweeper import sweep_expired_once
from tests.conftest import MakeSale

# Ждём обновления с запасом: путь длиннее обычного запроса —
# триггер, NOTIFY, окно склейки, чтение, рассылка.
RECEIVE_TIMEOUT = 5.0

# Сколько ждать, доказывая, что обновления НЕ будет.
SILENCE_TIMEOUT = 1.0

# В тесте склейки окно заметно больше рабочего: все изменения гарантированно
# попадают в одну пачку, и тест не мигает.
COALESCE_WINDOW_MS = 700

ListenerFactory = Callable[..., Awaitable[SaleChangeListener]]


@asynccontextmanager
async def open_ws(path: str) -> AsyncIterator[Any]:
    """Подключиться к WebSocket приложения.

    Клиент открывается внутри теста, а не в фикстуре: `ASGIWebSocketTransport`
    создаёт группу задач в `__aenter__`, а anyio привязывает cancel scope
    к задаче. pytest-asyncio финализирует асинхронные фикстуры отдельной
    задачей, и выход из контекста падал бы с «Attempted to exit cancel scope
    in a different task».
    """
    from app.main import app

    async with (
        AsyncClient(transport=ASGIWebSocketTransport(app), base_url="http://test") as client,
        aconnect_ws(path, client) as websocket,
    ):
        yield websocket


def find_disconnect(error: BaseException) -> WebSocketDisconnect | None:
    """Достать WebSocketDisconnect из группы исключений.

    `aconnect_ws` работает через группу задач anyio, поэтому ошибка приходит
    завёрнутой в ExceptionGroup.
    """
    if isinstance(error, WebSocketDisconnect):
        return error
    if isinstance(error, BaseExceptionGroup):
        for nested in error.exceptions:
            found = find_disconnect(nested)
            if found is not None:
                return found
    return None


@pytest_asyncio.fixture(loop_scope="session")
async def start_listener(test_database: str) -> AsyncIterator[ListenerFactory]:
    """Поднимает слушателя на свежем хабе и подменяет им хаб эндпоинта.

    Свой хаб на тест: подписки и накопленные сигналы не протекают между тестами.
    """
    import app.api.routes.live as live_route

    original_hub = live_route.hub
    tasks: list[asyncio.Task[None]] = []
    listeners: list[SaleChangeListener] = []

    async def factory(*, window_ms: int = 0) -> SaleChangeListener:
        test_hub = SaleHub()
        live_route.hub = test_hub
        listener = SaleChangeListener(test_hub, window_ms=window_ms)
        tasks.append(asyncio.create_task(listener.run(), name="test-live-updates"))
        # Ждём, пока LISTEN действительно установлен, иначе первые сигналы
        # пройдут мимо.
        async with asyncio.timeout(RECEIVE_TIMEOUT):
            while listener.backend_pid is None:
                await asyncio.sleep(0.01)
        listeners.append(listener)
        return listener

    try:
        yield factory
    finally:
        live_route.hub = original_hub
        for task in tasks:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


async def receive_snapshot(websocket: Any, timeout: float = RECEIVE_TIMEOUT) -> dict:
    async with asyncio.timeout(timeout):
        return await websocket.receive_json()


async def create_buyer(session: AsyncSession) -> UUID:
    user_id = await session.scalar(
        text("insert into users (email, role) values (:email, 'buyer') returning id"),
        {"email": f"live-{uuid4().hex[:12]}@example.com"},
    )
    await session.commit()
    return user_id


async def test_snapshot_is_sent_on_connect(
    start_listener: ListenerFactory, make_sale: MakeSale
) -> None:
    await start_listener()
    sale_id = await make_sale(quantity=5, sold=1, held=1)

    async with open_ws(f"/ws/sales/{sale_id}") as websocket:
        snapshot = await receive_snapshot(websocket)

    assert snapshot["sale_id"] == str(sale_id)
    assert (snapshot["quantity"], snapshot["sold"], snapshot["held"]) == (5, 1, 1)
    assert snapshot["remaining"] == 3
    assert snapshot["status"] == "active"
    assert snapshot["server_time"]


async def test_unknown_sale_is_closed(start_listener: ListenerFactory) -> None:
    await start_listener()

    with pytest.raises((WebSocketDisconnect, BaseExceptionGroup)) as closed:
        async with open_ws(f"/ws/sales/{uuid4()}") as websocket:
            await receive_snapshot(websocket, timeout=SILENCE_TIMEOUT)

    disconnect = find_disconnect(closed.value)
    assert disconnect is not None, f"ожидалось закрытие соединения, получено {closed.value!r}"
    assert disconnect.code == SALE_NOT_FOUND_CODE


async def test_two_clients_get_update_after_reserve(
    start_listener: ListenerFactory,
    session: AsyncSession,
    make_sale: MakeSale,
) -> None:
    await start_listener()
    sale_id = await make_sale(quantity=5)
    user_id = await create_buyer(session)

    async with (
        open_ws(f"/ws/sales/{sale_id}") as first,
        open_ws(f"/ws/sales/{sale_id}") as second,
    ):
        assert (await receive_snapshot(first))["remaining"] == 5
        assert (await receive_snapshot(second))["remaining"] == 5

        await create_reservation(session, sale_id=sale_id, user_id=user_id)

        first_update = await receive_snapshot(first)
        second_update = await receive_snapshot(second)

    for update in (first_update, second_update):
        assert update["remaining"] == 4
        assert update["held"] == 1


async def test_update_after_cancel(
    start_listener: ListenerFactory,
    session: AsyncSession,
    make_sale: MakeSale,
) -> None:
    await start_listener()
    sale_id = await make_sale(quantity=5)
    user_id = await create_buyer(session)
    reservation = await create_reservation(session, sale_id=sale_id, user_id=user_id)

    async with open_ws(f"/ws/sales/{sale_id}") as websocket:
        assert (await receive_snapshot(websocket))["remaining"] == 4

        await cancel_reservation(session, reservation_id=reservation.id, user_id=user_id)

        update = await receive_snapshot(websocket)

    assert update["remaining"] == 5
    assert update["held"] == 0


async def test_update_after_sweeper(
    start_listener: ListenerFactory,
    session: AsyncSession,
    make_sale: MakeSale,
) -> None:
    await start_listener()
    sale_id = await make_sale(quantity=5)
    user_id = await create_buyer(session)
    reservation = await create_reservation(session, sale_id=sale_id, user_id=user_id)
    await session.execute(
        text("""
            update reservations set expires_at = now() - interval '1 minute'
            where id = :reservation_id
        """),
        {"reservation_id": reservation.id},
    )
    await session.commit()

    async with open_ws(f"/ws/sales/{sale_id}") as websocket:
        # Первый снимок может прийти уже после правки expires_at — забираем его
        # и только потом запускаем свипер.
        await receive_snapshot(websocket)

        released = await sweep_expired_once(session)
        assert released == 1

        update = await receive_snapshot(websocket)

    assert update["remaining"] == 5
    assert update["held"] == 0


async def test_no_update_when_reserve_refused(
    start_listener: ListenerFactory,
    session: AsyncSession,
    make_sale: MakeSale,
) -> None:
    """Отказ откатывает транзакцию, а NOTIFY при откате не доставляется."""
    await start_listener()
    sale_id = await make_sale(quantity=1, sold=1)
    user_id = await create_buyer(session)

    async with open_ws(f"/ws/sales/{sale_id}") as websocket:
        assert (await receive_snapshot(websocket))["remaining"] == 0

        from app.services.reservations import ReserveError

        with pytest.raises(ReserveError):
            await create_reservation(session, sale_id=sale_id, user_id=user_id)

        with pytest.raises(TimeoutError):
            await receive_snapshot(websocket, timeout=SILENCE_TIMEOUT)


async def test_burst_is_coalesced_into_one_update(
    start_listener: ListenerFactory,
    session: AsyncSession,
    make_sale: MakeSale,
) -> None:
    """Три брони внутри окна склейки дают одно обновление, а не три.

    Окно в этом тесте намеренно большое: все три резерва заведомо попадают
    в одну пачку, поэтому результат не зависит от скорости машины.
    """
    await start_listener(window_ms=COALESCE_WINDOW_MS)
    sale_id = await make_sale(quantity=10)
    buyers = [await create_buyer(session) for _ in range(3)]

    async with open_ws(f"/ws/sales/{sale_id}") as websocket:
        assert (await receive_snapshot(websocket))["remaining"] == 10

        for user_id in buyers:
            await create_reservation(session, sale_id=sale_id, user_id=user_id)

        update = await receive_snapshot(websocket)
        # Снимок перечитан из БД, поэтому в одном сообщении виден итог всех трёх.
        assert update["remaining"] == 7
        assert update["held"] == 3

        # Больше ничего: три сигнала склеились в одну рассылку.
        with pytest.raises(TimeoutError):
            await receive_snapshot(websocket, timeout=SILENCE_TIMEOUT)


async def test_listener_reconnects_and_resends_snapshot(
    start_listener: ListenerFactory,
    session: AsyncSession,
    make_sale: MakeSale,
) -> None:
    """Соединение слушателя убито: после переподключения клиент получает снимок.

    Сигналы за время разрыва потеряны, поэтому слушатель досылает свежее
    состояние всем, на кого есть подписка.
    """
    listener = await start_listener()
    sale_id = await make_sale(quantity=5)
    user_id = await create_buyer(session)

    async with open_ws(f"/ws/sales/{sale_id}") as websocket:
        assert (await receive_snapshot(websocket))["remaining"] == 5

        killed_pid = listener.backend_pid
        assert killed_pid is not None
        connects_before = listener.connects

        # Меняем остаток, пока слушателя нет: этот сигнал гарантированно потерян.
        await session.execute(text("select pg_terminate_backend(:pid)"), {"pid": killed_pid})
        await session.commit()
        await create_reservation(session, sale_id=sale_id, user_id=user_id)

        async with asyncio.timeout(RECEIVE_TIMEOUT):
            while listener.connects == connects_before:
                await asyncio.sleep(0.05)

        update = await receive_snapshot(websocket)

    assert listener.backend_pid != killed_pid
    assert update["remaining"] == 4, "после переподключения снимок не досылается"
    assert update["held"] == 1


async def test_asgi_transport_is_available() -> None:
    """Страховка от молчаливой подмены транспорта обычным ASGITransport."""
    assert issubclass(ASGIWebSocketTransport, ASGITransport)
