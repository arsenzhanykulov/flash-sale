"""outbox delivery fields

Поля для доставки писем из outbox (ADR-024).

`locked_until` — аренда строки. Отправитель «занимает» письмо на время
отправки; если процесс упал, по истечении аренды строка сама возвращается
в работу, а не висит занятой навсегда.

`next_attempt_at` — когда можно пробовать снова. Нарастающая пауза хранится
в БД, а не в памяти: перезапуск не должен сбрасывать прогресс повторов.

`last_error` — чтобы причина отказа была видна в данных, а не только в логах.

Revision ID: d128d7c17202
Revises: 79d6f4bd15b5
Create Date: 2026-10-11 19:24:09.610034+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d128d7c17202"
down_revision: str | None = "79d6f4bd15b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "outbox",
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.add_column("outbox", sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("outbox", sa.Column("last_error", sa.Text(), nullable=True))
    # Выборка писем к отправке: только pending, их всегда немного.
    op.execute("""
        create index ix_outbox_pending_next_attempt_at
            on outbox (next_attempt_at)
            where status = 'pending'
    """)


def downgrade() -> None:
    op.drop_index("ix_outbox_pending_next_attempt_at", table_name="outbox")
    op.drop_column("outbox", "last_error")
    op.drop_column("outbox", "locked_until")
    op.drop_column("outbox", "next_attempt_at")
