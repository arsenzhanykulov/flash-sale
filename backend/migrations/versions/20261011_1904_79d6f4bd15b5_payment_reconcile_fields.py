"""payment reconcile fields

Поля и индекс для сверки зависших платежей (ADR-023).

`reconcile_attempts` живёт в БД, а не в памяти процесса: нарастающий интервал
проверок не должен сбрасываться при перезапуске backend — иначе после каждого
рестарта зависшие платежи опрашивались бы заново с минимальной паузой.

Частичный индекс по `status = 'pending'`: сверка смотрит только на незавершённые
платежи, а их всегда немного по сравнению с историей.

Revision ID: 79d6f4bd15b5
Revises: 42c06003e357
Create Date: 2026-10-11 19:04:33.221564+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "79d6f4bd15b5"
down_revision: str | None = "42c06003e357"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "payments",
        sa.Column(
            "reconcile_attempts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "reconcile_attempts_not_negative", "payments", "reconcile_attempts >= 0"
    )
    # NULLS FIRST: платежи, которые ещё не проверялись, идут первыми.
    op.execute("""
        create index ix_payments_pending_last_checked_at
            on payments (last_checked_at nulls first)
            where status = 'pending'
    """)


def downgrade() -> None:
    op.drop_index("ix_payments_pending_last_checked_at", table_name="payments")
    op.drop_constraint("ck_payments_reconcile_attempts_not_negative", "payments")
    op.drop_column("payments", "reconcile_attempts")
