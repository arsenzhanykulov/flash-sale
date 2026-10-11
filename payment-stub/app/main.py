"""Точка входа заглушки платёжного провайдера.

Отдельный сервис, а не часть backend (ADR-005, ADR-021): только так
проверяются сетевые сбои, асинхронный ответ вебхуком и независимый
перезапуск «чужой» системы.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import router as api_router
from app.processing import PaymentProcessor
from app.storage import Storage
from app.ui import router as ui_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    storage = Storage()
    await storage.connect()
    processor = PaymentProcessor(storage)

    app.state.storage = storage
    app.state.processor = processor

    # Задержка живёт в памяти, а срок — в БД: перезапуск во время задержки
    # не должен оставлять платёж висеть навсегда.
    recovered = await processor.recover_pending()
    if recovered:
        logger.info("восстановлено отложенных платежей: %d", recovered)

    try:
        yield
    finally:
        await processor.shutdown()
        await storage.close()


def create_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(name)s: %(message)s")

    app = FastAPI(
        title="Payment stub",
        version="0.1.0",
        description="Эмулятор платёжного провайдера: approve / decline / hang",
        lifespan=lifespan,
    )
    app.include_router(api_router)
    app.include_router(ui_router)
    return app


app = create_app()
