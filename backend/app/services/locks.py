"""Захват строк для операций, меняющих счётчики распродажи.

Инвариант 10: порядок захвата во всех операциях один —
`sales` → `reservations` → `orders` → `payments`.

Правило собрано здесь, а не продублировано по сервисам, чтобы следующая
операция не завела новую инверсию порядка: именно она дала дедлок на
шаге 2.3 (ADR-018).
"""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

LOCK_SALE_SQL = text("select id from sales where id = :sale_id for update")


async def lock_sale(session: AsyncSession, sale_id: UUID) -> None:
    """Взять строку распродажи — первый шаг любой операции с её счётчиками.

    Ждёт освобождения, а не пропускает занятую строку: транзакции резерва
    и отмены короткие, а ожидание сохраняет простую гарантию по срокам
    (ADR-019).
    """
    await session.execute(LOCK_SALE_SQL, {"sale_id": sale_id})
