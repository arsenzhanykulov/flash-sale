"""notify on sale change

Сигнал о смене состояния распродажи отправляет сама БД (ADR-020).

Почему триггер, а не вызов в сервисах: любая новая операция — оплата, закрытие
распродажи — обязана уведомить подписчиков, и забыть строчку в новом коде легко.
Триггер даёт гарантию от БД: кто бы и откуда ни изменил счётчики, сигнал уйдёт.

Payload — только id распродажи. Получатель перечитывает состояние из БД сам,
поэтому при обгоне сигналов остаток не «прыгает назад»: в рассылку уходит то,
что в базе сейчас.

NOTIFY внутри транзакции Postgres доставляет только при коммите, а при откате
выбрасывает — значит отказ резерва (sold_out, already_reserved) сигнала не даёт.
Это и надёжнее отправки из кода после коммита: там между коммитом
и уведомлением есть окно, в котором процесс может умереть.

Revision ID: 42c06003e357
Revises: 223134f5b6cc
Create Date: 2026-10-10 17:39:30.395982+00:00

"""

from collections.abc import Sequence

from alembic import op

revision: str = "42c06003e357"
down_revision: str | None = "223134f5b6cc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHANNEL = "sale_changed"

# Поля, из которых складывается публичный снимок распродажи: остаток
# (quantity, sold, held) и статус (start_at, end_at, closed_at). Правки
# остального — например, цены из seed — витрину не меняют и сигнала не дают.
WATCHED_COLUMNS = ("quantity", "sold", "held", "start_at", "end_at", "closed_at")


def _tuple(prefix: str) -> str:
    return ", ".join(f"{prefix}.{column}" for column in WATCHED_COLUMNS)


def upgrade() -> None:
    op.execute(f"""
        create or replace function notify_sale_changed() returns trigger
        language plpgsql as $$
        begin
            perform pg_notify('{CHANNEL}', new.id::text);
            return null;
        end;
        $$
    """)

    # AFTER UPDATE ... FOR EACH ROW: одинаковые (канал, payload) внутри одной
    # транзакции Postgres склеивает сам, поэтому транзакция, тронувшая строку
    # дважды, даёт один сигнал.
    op.execute(f"""
        create trigger sale_changed_notify
        after update on sales
        for each row
        when (({_tuple("old")}) is distinct from ({_tuple("new")}))
        execute function notify_sale_changed()
    """)


def downgrade() -> None:
    op.execute("drop trigger if exists sale_changed_notify on sales")
    op.execute("drop function if exists notify_sale_changed()")
