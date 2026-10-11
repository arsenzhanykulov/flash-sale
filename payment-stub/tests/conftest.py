"""Фикстуры тестов заглушки.

Каждый тест получает свой файл SQLite и свой приёмник вебхуков: сеть
не нужна, вебхуки ловятся через ASGI-транспорт httpx.
"""

import hashlib
import hmac
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from app.config import get_settings
from app.processing import PaymentProcessor
from app.storage import Storage

# Внутри тестов паузы повторов крошечные: проверяем поведение, а не терпение.
TEST_RETRY_BASE_MS = 10

CALLBACK_URL = "http://receiver.test/webhook"


@dataclass
class ReceivedWebhook:
    body: bytes
    signature: str | None

    @property
    def payload(self) -> dict:
        return json.loads(self.body)

    def signature_matches(self, secret: str) -> bool:
        """Независимый пересчёт подписи от принятых байтов."""
        expected = hmac.new(secret.encode(), self.body, hashlib.sha256).hexdigest()
        return self.signature == f"sha256={expected}"


@dataclass
class WebhookReceiver:
    """Приёмник вебхуков. `responses` задаёт коды ответов по порядку."""

    responses: list[int] = field(default_factory=list)
    received: list[ReceivedWebhook] = field(default_factory=list)

    def next_status(self) -> int:
        if not self.responses:
            return 200
        # Последний код повторяется, пока не кончатся попытки.
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

    async def __call__(self, scope, receive, send) -> None:
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break

        headers = {key.decode().lower(): value.decode() for key, value in scope["headers"]}
        self.received.append(ReceivedWebhook(body=body, signature=headers.get("x-signature")))

        await send({"type": "http.response.start", "status": self.next_status(), "headers": []})
        await send({"type": "http.response.body", "body": b""})


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBHOOK_RETRY_BASE_MS", str(TEST_RETRY_BASE_MS))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def receiver() -> WebhookReceiver:
    return WebhookReceiver()


@pytest.fixture(autouse=True)
def webhook_transport(monkeypatch: pytest.MonkeyPatch, receiver: WebhookReceiver) -> None:
    """Вебхуки уходят в приёмник внутри процесса, а не в сеть.

    Подменяется именно `app.webhooks.http_client`, а не `httpx.AsyncClient`:
    второе задело бы и клиент, которым тесты обращаются к самой заглушке.
    """
    import app.webhooks as webhooks

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=receiver), **kwargs)

    monkeypatch.setattr(webhooks, "http_client", client_factory)


@pytest_asyncio.fixture
async def storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Storage]:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "payments.db"))
    get_settings.cache_clear()
    store = Storage()
    await store.connect()
    try:
        yield store
    finally:
        await store.close()


@pytest_asyncio.fixture
async def processor(storage: Storage) -> AsyncIterator[PaymentProcessor]:
    instance = PaymentProcessor(storage)
    try:
        yield instance
    finally:
        await instance.shutdown()


@pytest_asyncio.fixture
async def client(storage: Storage, processor: PaymentProcessor) -> AsyncIterator[httpx.AsyncClient]:
    """Клиент к приложению с уже поднятым хранилищем.

    lifespan не прогоняем: хранилище и процессор создают фикстуры, чтобы
    у каждого теста была своя база.
    """
    from app.main import create_app

    app = create_app()
    app.state.storage = storage
    app.state.processor = processor

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://stub.test"
    ) as http_client:
        yield http_client


CreatePayment = Callable[..., httpx.Response]


@pytest.fixture
def create_payment(client: httpx.AsyncClient):
    async def _create(
        *,
        key: str,
        amount_minor: int = 499_000,
        currency: str = "KGS",
        callback_url: str = CALLBACK_URL,
    ) -> httpx.Response:
        return await client.post(
            "/payments",
            json={
                "idempotency_key": key,
                "amount_minor": amount_minor,
                "currency": currency,
                "callback_url": callback_url,
            },
        )

    return _create
