"""paid orders index

Выручка магазина считается как сумма `amount_minor` оплаченных заказов
по распродаже (ADR-026). Без индекса такой подсчёт просматривает всю таблицу
заказов — после нагрузочного скрипта их будет много.

Индекс частичный: `status = 'paid'`. Заказы в `pending_payment` и `failed`
в выручку не входят, и держать их в индексе незачем.

Revision ID: de786e87e27c
Revises: d128d7c17202
Create Date: 2026-10-11 20:34:03.112447+00:00

"""

from collections.abc import Sequence

from alembic import op

revision: str = "de786e87e27c"
down_revision: str | None = "d128d7c17202"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        create index ix_orders_paid_sale_id
            on orders (sale_id)
            where status = 'paid'
    """)


def downgrade() -> None:
    op.drop_index("ix_orders_paid_sale_id", table_name="orders")
