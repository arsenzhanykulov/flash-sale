from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, optional_text, required_text, uuid_fk, uuid_pk


class Product(Base):
    __tablename__ = "products"

    id: Mapped[uuid_pk]
    shop_id: Mapped[uuid_fk] = mapped_column(ForeignKey("users.id"))
    name: Mapped[required_text]
    description: Mapped[optional_text]
    image_url: Mapped[optional_text]
