"""Разрешение платежей: по сроку, вручную и после перезапуска.

Фоновая задача живёт в памяти и перезапуск не переживает. Поэтому срок,
когда платёж должен разрешиться, хранится в БД (`payments.resolve_at`),
и при старте просроченные доделываются, а недождавшиеся — планируются
заново. Иначе перезапуск во время задержки оставлял бы платёж висеть
навсегда, хотя режим `hang` никто не включал.
"""

import asyncio
import logging

from app.config import PaymentMode
from app.storage import Payment, PaymentStatus, Storage, now
from app.webhooks import deliver_result

logger = logging.getLogger(__name__)

# Режим платежа → во что он превращается по истечении срока.
OUTCOMES: dict[PaymentMode, PaymentStatus] = {
    PaymentMode.APPROVE: PaymentStatus.SUCCEEDED,
    PaymentMode.DECLINE: PaymentStatus.DECLINED,
}


class PaymentProcessor:
    """Отложенные решения по платежам."""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage
        self._tasks: set[asyncio.Task[None]] = set()

    def _spawn(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        # Ссылку держим до завершения: иначе сборщик мусора может
        # уничтожить задачу на полпути.
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def shutdown(self) -> None:
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        # Дожидаемся, но не разбираемся: на остановке упавшая задача
        # ничего не меняет, а срок её платежа лежит в БД и будет доделан
        # при следующем старте.
        await asyncio.gather(*tasks, return_exceptions=True)

    def schedule(self, payment: Payment) -> None:
        """Запланировать автоматическое решение, если у платежа есть срок."""
        if payment.resolve_at is None:
            return
        delay = max(0.0, (payment.resolve_at - now()).total_seconds())
        self._spawn(self._resolve_after(payment.id, delay))

    async def _resolve_after(self, payment_id: str, delay: float) -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            await self.resolve_by_mode(payment_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("не удалось разрешить платёж %s", payment_id)

    async def resolve_by_mode(self, payment_id: str) -> Payment | None:
        """Довести платёж до результата, заданного его режимом."""
        payment = await self._storage.get(payment_id)
        if payment is None or payment.status is not PaymentStatus.PENDING:
            return None

        outcome = OUTCOMES.get(payment.mode)
        if outcome is None:
            # hang: решение только вручную.
            return None
        return await self.settle(payment_id, outcome)

    async def settle(self, payment_id: str, status: PaymentStatus) -> Payment | None:
        """Зафиксировать результат и отправить вебхук.

        Условный UPDATE внутри settle гарантирует, что результат применится
        один раз, даже если фоновая задача и ручное решение сойдутся.
        """
        settled = await self._storage.settle(payment_id, status)
        if settled is None:
            return None
        self._spawn(deliver_result(settled, self._storage))
        return settled

    async def recover_pending(self) -> int:
        """Доделать платежи, чья задержка пережила перезапуск.

        Просроченные разрешаются сразу, остальные планируются на остаток
        срока. Платежи в режиме hang сюда не попадают: у них нет resolve_at.
        """
        pending = await self._storage.pending_with_deadline()
        if not pending:
            return 0

        current = now()
        overdue = [payment for payment in pending if payment.resolve_at <= current]
        for payment in pending:
            self.schedule(payment)

        logger.info(
            "после старта доделываем отложенных платежей: %d (из них просрочено: %d)",
            len(pending),
            len(overdue),
        )
        return len(pending)
