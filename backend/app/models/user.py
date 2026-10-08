from enum import StrEnum

from sqlalchemy import CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at, required_text, status_in, uuid_pk


class UserRole(StrEnum):
    BUYER = "buyer"
    SHOP = "shop"


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(status_in("role", UserRole), name="role_known"),
        # В документе email хранится в lower-case — проверку держит сама БД,
        # чтобы регистр не зависел от того, через какой код пришла вставка.
        CheckConstraint("email = lower(email)", name="email_lowercase"),
    )

    id: Mapped[uuid_pk]
    email: Mapped[required_text] = mapped_column(unique=True)
    role: Mapped[required_text]
    created_at: Mapped[created_at]
