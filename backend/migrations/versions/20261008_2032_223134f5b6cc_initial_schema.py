"""initial schema

Первая миграция: схема строго по docs/DATA_MODEL.md.

Сгенерирована autogenerate и затем сверена с документом построчно.
Статусы — text с CHECK, без нативных Postgres ENUM: добавить значение
в ENUM нельзя внутри транзакции с другими изменениями, а CHECK меняется
обычной миграцией.

Revision ID: 223134f5b6cc
Revises:
Create Date: 2026-10-08 20:32:51.370453+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "223134f5b6cc"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Допустимые статусы. Повторяют StrEnum в app/models — CHECK и код должны
# совпадать, расхождение здесь поймают тесты схемы.
USER_ROLES = "'buyer', 'shop'"
RESERVATION_STATUSES = "'held', 'paying', 'paid', 'expired', 'failed', 'cancelled'"
ORDER_STATUSES = "'pending_payment', 'paid', 'failed'"
PAYMENT_STATUSES = "'pending', 'succeeded', 'declined'"
OUTBOX_STATUSES = "'pending', 'sent', 'failed'"

# Статусы, при которых бронь занимает единицу товара (ADR-008).
ACTIVE_RESERVATION_STATUSES = "'held', 'paying', 'paid'"


def upgrade() -> None:
    op.create_table(
        "outbox",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("dedup_key", sa.Text(), nullable=False),
        sa.Column("recipient", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            f"status IN ({OUTBOX_STATUSES})",
            name=op.f("ck_outbox_status_known"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox")),
        # Инвариант 6: ровно одно письмо на событие.
        sa.UniqueConstraint("dedup_key", name=op.f("uq_outbox_dedup_key")),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(f"role IN ({USER_ROLES})", name=op.f("ck_users_role_known")),
        # email хранится в lower-case — правило держит БД, а не только код.
        sa.CheckConstraint("email = lower(email)", name=op.f("ck_users_email_lowercase")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )

    op.create_table(
        "products",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("shop_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["shop_id"],
            ["users.id"],
            name=op.f("fk_products_shop_id_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_products")),
    )

    op.create_table(
        "sales",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("product_id", sa.UUID(), nullable=False),
        # Инвариант 8: деньги — целые в минимальных единицах.
        sa.Column("price_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.CHAR(length=3), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("sold", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("held", sa.Integer(), server_default=sa.text("0"), nullable=False),
        # Инвариант 2: решает серверное время, start_at/end_at сравниваются с now() в БД.
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("end_at > start_at", name=op.f("ck_sales_period_valid")),
        sa.CheckConstraint("price_minor > 0", name=op.f("ck_sales_price_positive")),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_sales_quantity_positive")),
        # Инвариант 1, последняя линия защиты от оверсейла: держится даже при баге в коде.
        sa.CheckConstraint(
            "sold >= 0 AND held >= 0 AND sold + held <= quantity",
            name=op.f("ck_sales_not_oversold"),
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_sales_product_id_products"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sales")),
    )

    op.create_table(
        "reservations",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("sale_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        # Инвариант 3: held живёт 10 минут. Срок проставляет сервис.
        # Инвариант 4: для paying не учитывается.
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"status IN ({RESERVATION_STATUSES})",
            name=op.f("ck_reservations_status_known"),
        ),
        sa.ForeignKeyConstraint(
            ["sale_id"],
            ["sales.id"],
            name=op.f("fk_reservations_sale_id_sales"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_reservations_user_id_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reservations")),
    )
    # Свипер ищет held с истёкшим expires_at.
    op.create_index(
        "ix_reservations_status_expires_at",
        "reservations",
        ["status", "expires_at"],
        unique=False,
    )
    # ADR-008: одна активная бронь на покупателя в распродаже. Частичный —
    # завершённые брони не мешают купить снова.
    op.create_index(
        "uq_reservations_sale_id_user_id_active",
        "reservations",
        ["sale_id", "user_id"],
        unique=True,
        postgresql_where=sa.text(f"status IN ({ACTIVE_RESERVATION_STATUSES})"),
    )

    op.create_table(
        "orders",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("reservation_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("sale_id", sa.UUID(), nullable=False),
        # Сумма фиксируется при создании заказа.
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.CHAR(length=3), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            f"status IN ({ORDER_STATUSES})",
            name=op.f("ck_orders_status_known"),
        ),
        sa.CheckConstraint("amount_minor > 0", name=op.f("ck_orders_amount_positive")),
        sa.ForeignKeyConstraint(
            ["reservation_id"],
            ["reservations.id"],
            name=op.f("fk_orders_reservation_id_reservations"),
        ),
        sa.ForeignKeyConstraint(
            ["sale_id"],
            ["sales.id"],
            name=op.f("fk_orders_sale_id_sales"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_orders_user_id_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orders")),
        # Инвариант 5: один заказ на бронь.
        sa.UniqueConstraint("reservation_id", name=op.f("uq_orders_reservation_id")),
    )

    op.create_table(
        "payments",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("order_id", sa.UUID(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("provider_payment_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"status IN ({PAYMENT_STATUSES})",
            name=op.f("ck_payments_status_known"),
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name=op.f("fk_payments_order_id_orders"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payments")),
        # Инвариант 5: один платёж на заказ, повторный клик не дублирует.
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_payments_idempotency_key")),
        sa.UniqueConstraint("order_id", name=op.f("uq_payments_order_id")),
    )


def downgrade() -> None:
    # Порядок обратный созданию: сначала таблицы, на которые ссылаются другие.
    op.drop_table("payments")
    op.drop_table("orders")
    op.drop_index(
        "uq_reservations_sale_id_user_id_active",
        table_name="reservations",
        postgresql_where=sa.text(f"status IN ({ACTIVE_RESERVATION_STATUSES})"),
    )
    op.drop_index("ix_reservations_status_expires_at", table_name="reservations")
    op.drop_table("reservations")
    op.drop_table("sales")
    op.drop_table("products")
    op.drop_table("users")
    op.drop_table("outbox")
