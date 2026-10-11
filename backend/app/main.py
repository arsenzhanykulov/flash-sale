"""Точка входа FastAPI-приложения."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.config import get_settings
from app.core.db import reset_engine
from app.services.live_updates import SaleChangeListener, hub
from app.services.outbox import run_outbox_sender
from app.services.payments import run_reconciler
from app.services.sale_closer import run_sale_closer
from app.services.sweeper import run_sweeper

logger = logging.getLogger(__name__)


async def stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    background: list[asyncio.Task[None]] = []

    if settings.sweeper_enabled:
        background.append(asyncio.create_task(run_sweeper(), name="sweeper"))
    else:
        logger.warning(
            "SWEEPER_ENABLED=false: истёкшие брони не возвращаются на витрину, "
            "занятые единицы останутся занятыми"
        )

    if settings.reconciler_enabled:
        background.append(asyncio.create_task(run_reconciler(), name="reconciler"))
    else:
        logger.warning(
            "RECONCILER_ENABLED=false: платежи без вебхука останутся в pending, "
            "заказы не будут досчитаны"
        )

    if settings.outbox_enabled:
        background.append(asyncio.create_task(run_outbox_sender(), name="outbox-sender"))
    else:
        logger.warning("OUTBOX_ENABLED=false: письма останутся в outbox и не будут отправлены")

    if settings.sale_closer_enabled:
        background.append(asyncio.create_task(run_sale_closer(), name="sale-closer"))
    else:
        logger.warning(
            "SALE_CLOSER_ENABLED=false: закончившиеся распродажи останутся "
            "открытыми, брони в них не будут сняты"
        )

    listener = SaleChangeListener(hub)
    background.append(asyncio.create_task(listener.run(), name="live-updates"))

    try:
        yield
    finally:
        for task in background:
            await stop(task)
        # Пул закрываем последним: фоновые задачи уже остановлены.
        await reset_engine()


def create_app() -> FastAPI:
    settings = get_settings()

    # Корневой логгер по умолчанию на WARNING, поэтому INFO приложения никуда
    # не попадал: о работе свипера нельзя было узнать из логов. Логгеры uvicorn
    # настраивает сам и не пропускают записи в корневой, так что дублей нет.
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)-8s %(name)s: %(message)s",
    )

    app = FastAPI(title="Flash Sale API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api_router)
    return app


app = create_app()
