"""Упрощённый вход по email (ADR-007)."""

from uuid import uuid4

from httpx import AsyncClient
from pytest import MonkeyPatch
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import create_access_token


async def login(client: AsyncClient, email: str) -> dict:
    response = await client.post("/api/auth/login", json={"email": email})
    assert response.status_code == 200, response.text
    return response.json()


async def count_users(session: AsyncSession, email: str) -> int:
    await session.rollback()  # читаем свежие данные, а не снимок своей транзакции
    return await session.scalar(
        text("select count(*) from users where email = :email"), {"email": email}
    )


async def test_login_creates_buyer(client: AsyncClient, session: AsyncSession) -> None:
    email = f"new-{uuid4().hex[:12]}@example.com"

    body = await login(client, email)

    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["user"]["email"] == email
    assert body["user"]["role"] == "buyer"
    assert await count_users(session, email) == 1


async def test_login_twice_does_not_duplicate_user(
    client: AsyncClient, session: AsyncSession
) -> None:
    email = f"repeat-{uuid4().hex[:12]}@example.com"

    first = await login(client, email)
    second = await login(client, email)

    assert first["user"]["id"] == second["user"]["id"]
    assert await count_users(session, email) == 1


async def test_login_normalizes_email_case(client: AsyncClient, session: AsyncSession) -> None:
    suffix = uuid4().hex[:12]
    lower_email = f"mixed-{suffix}@example.com"

    first = await login(client, lower_email)
    second = await login(client, f"Mixed-{suffix}@Example.COM")

    assert first["user"]["id"] == second["user"]["id"]
    assert second["user"]["email"] == lower_email
    # В базе ровно один пользователь, и email сохранён в lower-case.
    assert await count_users(session, lower_email) == 1


async def test_login_keeps_existing_role(client: AsyncClient, session: AsyncSession) -> None:
    """Вход магазина не превращает его в покупателя."""
    email = f"shop-{uuid4().hex[:12]}@example.com"
    await session.execute(
        text("insert into users (email, role) values (:email, 'shop')"), {"email": email}
    )
    await session.commit()

    body = await login(client, email)

    assert body["user"]["role"] == "shop"
    await session.rollback()
    role = await session.scalar(
        text("select role from users where email = :email"), {"email": email}
    )
    assert role == "shop"


async def test_me_returns_current_user(client: AsyncClient) -> None:
    body = await login(client, f"me-{uuid4().hex[:12]}@example.com")

    response = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )

    assert response.status_code == 200
    assert response.json() == body["user"]


async def test_me_without_token_is_401(client: AsyncClient) -> None:
    response = await client.get("/api/auth/me")
    assert response.status_code == 401


async def test_me_with_broken_token_is_401(client: AsyncClient) -> None:
    response = await client.get("/api/auth/me", headers={"Authorization": "Bearer not-a-token"})
    assert response.status_code == 401


async def test_me_with_expired_token_is_401(client: AsyncClient, monkeypatch: MonkeyPatch) -> None:
    # Отрицательный срок жизни — токен просрочен в момент выдачи.
    monkeypatch.setenv("ACCESS_TOKEN_TTL_MINUTES", "-1")
    get_settings.cache_clear()
    try:
        expired_token = create_access_token(uuid4())
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()

    response = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {expired_token}"}
    )

    assert response.status_code == 401


async def test_login_rejects_invalid_email(client: AsyncClient) -> None:
    response = await client.post("/api/auth/login", json={"email": "не-email"})
    assert response.status_code == 422
