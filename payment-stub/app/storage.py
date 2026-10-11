"""Хранилище заглушки: SQLite.

Отдельная база от основного сервиса — это чужая система (ADR-021). Миграций
нет намеренно: схема создаётся при старте, историю чужой БД мы не ведём.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

import aiosqlite

from app.config import PaymentMode, get_settings

SCHEMA = """
create table if not exists payments (
    id text primary key,
    idempotency_key text not null unique,
    amount_minor integer not null,
    currency text not null,
    callback_url text not null,
    status text not null,
    mode text not null,
    -- Когда платёж должен разрешиться. NULL для hang: он ждёт ручного
    -- вмешательства. По этому полю при старте доделываются платежи,
    -- чья задержка пережила перезапуск.
    resolve_at text,
    created_at text not null,
    resolved_at text,
    webhook_attempts integer not null default 0,
    webhooks_sent integer not null default 0
);

create index if not exists payments_pending_resolve_at
    on payments (status, resolve_at);

create table if not exists settings (
    key text primary key,
    value text not null
);
"""

MODE_KEY = "mode"


class PaymentStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    DECLINED = "declined"


@dataclass(frozen=True)
class Payment:
    id: str
    idempotency_key: str
    amount_minor: int
    currency: str
    callback_url: str
    status: PaymentStatus
    mode: PaymentMode
    resolve_at: datetime | None
    created_at: datetime
    resolved_at: datetime | None
    webhook_attempts: int
    webhooks_sent: int


@dataclass(frozen=True)
class ModeSettings:
    mode: PaymentMode
    delay_ms: int
    duplicate_webhooks: bool


class AmountMismatch(Exception):
    """Тот же ключ идемпотентности пришёл с другой суммой или валютой."""


def now() -> datetime:
    return datetime.now(UTC)


def _parse_time(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _to_payment(row: aiosqlite.Row) -> Payment:
    return Payment(
        id=row["id"],
        idempotency_key=row["idempotency_key"],
        amount_minor=row["amount_minor"],
        currency=row["currency"],
        callback_url=row["callback_url"],
        status=PaymentStatus(row["status"]),
        mode=PaymentMode(row["mode"]),
        resolve_at=_parse_time(row["resolve_at"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        resolved_at=_parse_time(row["resolved_at"]),
        webhook_attempts=row["webhook_attempts"],
        webhooks_sent=row["webhooks_sent"],
    )


class Storage:
    def __init__(self, database_path: str | None = None) -> None:
        self.path = database_path or get_settings().database_path
        self._connection: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)

        self._connection = await aiosqlite.connect(self.path)
        self._connection.row_factory = aiosqlite.Row
        # WAL и ожидание блокировки: без них параллельные запросы к SQLite
        # падают с «database is locked».
        await self._connection.execute("pragma journal_mode=WAL")
        await self._connection.execute("pragma busy_timeout=5000")
        await self._connection.executescript(SCHEMA)
        await self._connection.commit()

        settings = get_settings()
        await self._ensure_mode(
            ModeSettings(
                mode=settings.payment_mode,
                delay_ms=settings.payment_delay_ms,
                duplicate_webhooks=settings.duplicate_webhooks,
            )
        )

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("хранилище не подключено")
        return self._connection

    # --- режим ---

    async def _ensure_mode(self, defaults: ModeSettings) -> None:
        """Записать режим по умолчанию, не перетирая сохранённый."""
        await self.db.execute(
            "insert into settings (key, value) values (?, ?) on conflict (key) do nothing",
            (
                MODE_KEY,
                json.dumps(
                    {
                        "mode": defaults.mode.value,
                        "delay_ms": defaults.delay_ms,
                        "duplicate_webhooks": defaults.duplicate_webhooks,
                    }
                ),
            ),
        )
        await self.db.commit()

    async def get_mode(self) -> ModeSettings:
        async with self.db.execute(
            "select value from settings where key = ?", (MODE_KEY,)
        ) as cursor:
            row = await cursor.fetchone()
        stored = json.loads(row["value"])
        return ModeSettings(
            mode=PaymentMode(stored["mode"]),
            delay_ms=stored["delay_ms"],
            duplicate_webhooks=stored["duplicate_webhooks"],
        )

    async def set_mode(self, mode: ModeSettings) -> None:
        await self.db.execute(
            "update settings set value = ? where key = ?",
            (
                json.dumps(
                    {
                        "mode": mode.mode.value,
                        "delay_ms": mode.delay_ms,
                        "duplicate_webhooks": mode.duplicate_webhooks,
                    }
                ),
                MODE_KEY,
            ),
        )
        await self.db.commit()

    # --- платежи ---

    async def create_or_get(
        self,
        *,
        idempotency_key: str,
        amount_minor: int,
        currency: str,
        callback_url: str,
        mode: ModeSettings,
    ) -> tuple[Payment, bool]:
        """Создать платёж или вернуть существующий по ключу идемпотентности.

        Возвращает (платёж, создан ли он сейчас). Идемпотентность держит
        UNIQUE-индекс, а не чтение перед записью: иначе два одновременных
        запроса с одним ключом создали бы два платежа.
        """
        existing = await self.get_by_key(idempotency_key)
        if existing is not None:
            if existing.amount_minor != amount_minor or existing.currency != currency:
                raise AmountMismatch
            return existing, False

        created_at = now()
        # hang ждёт ручного решения, поэтому срока у него нет.
        resolve_at = (
            None
            if mode.mode is PaymentMode.HANG
            else created_at + timedelta(milliseconds=mode.delay_ms)
        )
        payment_id = str(uuid4())

        try:
            await self.db.execute(
                """
                insert into payments (
                    id, idempotency_key, amount_minor, currency, callback_url,
                    status, mode, resolve_at, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payment_id,
                    idempotency_key,
                    amount_minor,
                    currency,
                    callback_url,
                    PaymentStatus.PENDING.value,
                    mode.mode.value,
                    None if resolve_at is None else resolve_at.isoformat(),
                    created_at.isoformat(),
                ),
            )
            await self.db.commit()
        except aiosqlite.IntegrityError:
            # Гонка: пока мы вставляли, тот же ключ создал кто-то ещё.
            await self.db.rollback()
            raced = await self.get_by_key(idempotency_key)
            if raced is None:
                raise
            if raced.amount_minor != amount_minor or raced.currency != currency:
                raise AmountMismatch from None
            return raced, False

        created = await self.get(payment_id)
        assert created is not None
        return created, True

    async def get(self, payment_id: str) -> Payment | None:
        async with self.db.execute("select * from payments where id = ?", (payment_id,)) as cursor:
            row = await cursor.fetchone()
        return None if row is None else _to_payment(row)

    async def get_by_key(self, idempotency_key: str) -> Payment | None:
        async with self.db.execute(
            "select * from payments where idempotency_key = ?", (idempotency_key,)
        ) as cursor:
            row = await cursor.fetchone()
        return None if row is None else _to_payment(row)

    async def list_payments(self, limit: int = 50) -> list[Payment]:
        async with self.db.execute(
            "select * from payments order by created_at desc limit ?", (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
        return [_to_payment(row) for row in rows]

    async def settle(self, payment_id: str, status: PaymentStatus) -> Payment | None:
        """Перевести pending-платёж в окончательный статус.

        Условие `status = 'pending'` в UPDATE — чтобы одновременные решения
        (фоновая задача и ручное) не применились дважды. Нет строки —
        значит кто-то успел раньше.
        """
        cursor = await self.db.execute(
            """
            update payments
            set status = ?, resolved_at = ?, resolve_at = null
            where id = ? and status = ?
            """,
            (status.value, now().isoformat(), payment_id, PaymentStatus.PENDING.value),
        )
        await self.db.commit()
        if cursor.rowcount == 0:
            return None
        return await self.get(payment_id)

    async def pending_with_deadline(self) -> list[Payment]:
        """Платежи, которые должны были разрешиться сами.

        Используется при старте: фоновая задача живёт в памяти и перезапуск
        не переживает, поэтому срок хранится в БД. hang сюда не попадает —
        у него resolve_at пустой.
        """
        async with self.db.execute(
            """
            select * from payments
            where status = ? and resolve_at is not null
            order by resolve_at
            """,
            (PaymentStatus.PENDING.value,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_to_payment(row) for row in rows]

    async def count_webhook_attempt(self, payment_id: str, *, delivered: bool) -> None:
        await self.db.execute(
            """
            update payments
            set webhook_attempts = webhook_attempts + 1,
                webhooks_sent = webhooks_sent + ?
            where id = ?
            """,
            (1 if delivered else 0, payment_id),
        )
        await self.db.commit()
