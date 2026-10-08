# Журнал разработки

Время — реальное, из `date '+%Y-%m-%d %H:%M %Z'`. Каждая запись — один завершённый шаг.

Формат записи:

```
## YYYY-MM-DD HH:MM — <короткое название шага>
**Сделано:** что именно появилось / изменилось
**Решения:** что выбрали и почему (ссылка на ADR, если есть)
**Агент:** что делал Claude, что поправил я (ошибки агента, отклонённые предложения)
**Проверка:** какие тесты / ручные проверки прошли
**Дальше:** следующий шаг
```

---

## <!-- Thu Oct  8 23:59:31 +06 2026 --> — Старт работы
**Сделано:** выбрано задание 4 «Флэш-распродажа», создан репозиторий, CLAUDE.md, DECISIONS.md, DATA_MODEL.md.
**Решения:** стек и ключевые подходы — ADR-001…ADR-010.
**Инструмент:** Claude Code в терминале PyCharm.
**Дальше:** docker-compose с Postgres и Mailpit, скелет backend.

## 2026-10-09 00:51 — Инфраструктура: Postgres + Mailpit
**Сделано:** `docker-compose.yml` (postgres:16 с healthcheck `pg_isready` и именованным
volume `pgdata`; mailpit v1.31.4 — SMTP 1025, веб-UI 8025, healthcheck `/mailpit readyz`),
`.env.example` со всеми переменными этих сервисов, README-заготовка, дополнен `.gitignore`
(Python / Node / IDE / `.env` с исключением `!.env.example`).
**Решения:**
- Версии образов пинятся (`postgres:16`, `axllent/mailpit:v1.31.4`) — воспроизводимость сборки;
  тег mailpit взят из Docker Hub, не по памяти.
- Healthcheck mailpit — встроенный `/mailpit readyz`: в образе нет curl.
- `TZ=UTC`/`PGTZ=UTC` у Postgres — инвариант 9 (время в БД только UTC).
- Порт Postgres на хосте — через `${POSTGRES_PORT:-5432}`; внутри docker-сети всегда 5432,
  поэтому `DATABASE_URL` не зависит от хостового порта.
- Ключ `version:` в compose не используется (устарел в Compose v2), вместо него `name: flash_sale`.
**Агент:** Claude подготовил план и файлы. Поправил я: попросил пинить версию mailpit
с проверкой тега через `docker pull`, а не по памяти; порт 5432 на хосте занят контейнером
из другого проекта (`ort-services-db-1`) — дефолт в compose оставили 5432, а локально
в `.env` поставили `POSTGRES_PORT=5433` (5433 предварительно проверен на занятость).
**Проверка:**
- `docker compose config --quiet` — конфиг валиден.
- `docker compose up -d` → оба контейнера `healthy` (postgres и mailpit).
- `psql` внутри контейнера: `now()` отдаёт `+00`, `TimeZone = UTC`, PostgreSQL 16.15.
- `psql` с хоста через проброшенный порт 5433 — подключение проходит.
- Mailpit: `GET /livez` и веб-UI — 200; тестовое письмо отправлено по SMTP на `localhost:1025`
  и видно в `GET /api/v1/messages` (после проверки ящик очищен).
- `docker compose down && up` — таблица, созданная до перезапуска, на месте: volume переживает
  пересоздание контейнеров.
- `git check-ignore .env` — игнорируется; `.env.example` в индекс попадает.
**Дальше:** скелет backend (FastAPI + SQLAlchemy 2 async + Alembic), сервис в compose,
первая миграция по `docs/DATA_MODEL.md`.

## 2026-10-09 01:44 — Скелет backend: FastAPI, /health, тесты на реальном Postgres
**Ветка:** `feat/backend-skeleton`
**Сделано:** `backend/` на uv (Python 3.12, `uv.lock` в репозитории), слои
`app/api` → `app/services` → `app/models` + `app/core` (`config.py`, `db.py`);
`GET /health` с реальной проверкой `select 1`; CORS из настроек; `backend/Dockerfile`
и сервис `backend` в compose (зависит от postgres `service_healthy`, порт 8000,
hot reload через volume); тесты на реальном Postgres в отдельной базе; `.env.example`
дополнен `BACKEND_PORT`, `CORS_ORIGINS`, `DB_ECHO`, `TEST_DATABASE_URL` (закомментирован).
**Решения:**
- Тестовая база — ADR-011: `flash_sale_test` на том же Postgres, пересоздаётся каждый прогон.
- `/health` не берёт сессию через `Depends`: ошибка подключения в зависимости вылетает
  до тела эндпоинта и FastAPI отдаёт 500, а нужен 503. Поэтому сессию открывает
  сервис `services/health.ping_db()`, он же ловит `SQLAlchemyError`/`OSError`.
- Engine создаётся лениво и кэшируется; `reset_engine()` закрывает пул и сбрасывает кэш.
  Благодаря ленивости тесты поднимают ASGI-приложение без прогона lifespan — не понадобилась
  зависимость `asgi-lifespan`.
- `pool_pre_ping=True` — иначе `/health` отвечал бы ok по мёртвому соединению из пула.
- `CORS_ORIGINS` — список через запятую: `NoDecode` + `field_validator`, иначе
  pydantic-settings требует JSON в `.env`.
- Один event loop на весь прогон (`asyncio_default_*_loop_scope = "session"`): кэшированный
  engine иначе оказался бы привязан к закрытому loop следующего теста.
- Образ собирается на `ghcr.io/astral-sh/uv` с `UV_PROJECT_ENVIRONMENT=/usr/local`, чтобы
  `uvicorn`/`pytest`/`ruff` были в PATH без `uv run`. `uv` на хост не ставился: `uv.lock`
  сгенерирован одноразовым `docker run` с тем же образом.
**Агент:** Claude предложил план и реализовал его. Поправил я (до начала работы, в плане):
(1) защита от сноса рабочей базы — проверка суффикса `_test` и несовпадения с `DATABASE_URL`,
падать с понятной ошибкой; (2) при подмене `DATABASE_URL` сбрасывать не только кэш настроек,
но и кэш engine, плюс проверять в тесте `select current_database()`; (3) добавить второй тест
на 503 при недоступной БД, ручную проверку с остановкой postgres оставить.
**Проверка:**
- `docker compose up --build` — все три сервиса healthy, backend ждёт healthy-postgres.
- `GET /health` → 200 `{"status":"ok","database":"ok"}`; `GET /docs` → 200;
  в OpenAPI у `/health` описаны оба ответа — 200 и 503.
- CORS: preflight с `Origin: http://localhost:5173` → `access-control-allow-origin`;
  с посторонним origin → 400 без разрешающего заголовка.
- `pytest -q` → 2 passed (оба теста на реальном Postgres).
  Тест проверяет `select current_database()` = `flash_sale_test`.
- Защиты тестовой базы проверены запуском: `TEST_DATABASE_URL` на рабочую базу →
  «имя тестовой базы 'flash_sale' должно заканчиваться на '_test'»; совпадение тестовой
  и рабочей (`demo_test`) → «тестовая и рабочая база совпадают». Рабочая база после всех
  прогонов цела, `flash_sale_test` удалена.
- `ruff check .` — All checks passed; `ruff format --check .` — 14 files already formatted.
- Ручная проверка 503: `docker compose stop postgres` → `/health` отдаёт 503
  `{"status":"error","database":"unavailable"}`; после `start postgres` снова 200
  без перезапуска backend.
- Hot reload: правка `app/main.py` → в логах `WatchFiles detected changes ... Reloading`.
**Дальше:** модели SQLAlchemy по `docs/DATA_MODEL.md`, Alembic и первая миграция
(включая CHECK `sold + held <= quantity` и частичный уникальный индекс по броням).

## 2026-10-09 02:39 — Модели SQLAlchemy и первая миграция
**Ветка:** `feat/db-schema`
**Сделано:** Alembic с async-конфигурацией (`alembic.ini`, `migrations/env.py`), модели всех
7 таблиц по `DATA_MODEL.md` (`app/models/`, по модулю на таблицу + `base.py`), первая миграция
`223134f5b6cc_initial_schema`; тестовая фикстура накатывает схему миграциями; 3 новых теста.
Зависимость `alembic` 1.20.0, `uv.lock` перегенерирован.
**Решения:** ADR-012 (два режима env.py, URL из настроек, `naming_convention`,
схема в тестах — миграциями, а не `create_all`).
- Статусы — `text` + CHECK, без нативных ENUM; значения живут в `StrEnum` рядом с моделью,
  CHECK собирается из него, чтобы схема и код не разъехались.
- `reservations.expires_at` — `NOT NULL` без server default: TTL брони это бизнес-правило,
  в схеме его смена требовала бы миграции.
- `updated_at` — `server_default now()` + ORM-`onupdate`, без триггера в БД; в явном SQL
  критичных операций `updated_at = now()` придётся писать руками.
- FK без `ON DELETE CASCADE`: брони, заказы и платежи каскадом не удаляются.
- Согласованы три добавления к `DATA_MODEL.md`, документ обновлён: `orders.currency NOT NULL`,
  `CHECK (email = lower(email))`, `CHECK (amount_minor > 0)`.
**Агент:** Claude спросил про три неоднозначности в документе до начала работы, а не додумал.
Autogenerate в Alembic 1.20 неожиданно подхватил и CHECK, и частичный индекс с `postgresql_where` —
терять ничего не пришлось, но миграция всё равно сверена с документом построчно и переписана
в читаемый вид (содержимое то же, `alembic check` это подтверждает). Сам нашёл и исправил изъян
в своём тесте: `session.rollback()` после ожидаемой ошибки сносил и данные фикстуры —
переделано на SAVEPOINT (`begin_nested`).
**Проверка:**
- `alembic upgrade head` на рабочей базе; `alembic current` → `223134f5b6cc (head)`;
  `alembic check` → «No new upgrade operations detected» (модели и БД совпадают).
- Схема осмотрена в psql: 11 CHECK на месте (включая
  `ck_sales_not_oversold`, `ck_sales_period_valid`, `ck_users_email_lowercase`,
  `ck_orders_amount_positive`), частичный индекс
  `uq_reservations_sale_id_user_id_active ... WHERE status = ANY (ARRAY['held','paying','paid'])`,
  индекс `ix_reservations_status_expires_at`, уникальные `uq_orders_reservation_id`,
  `uq_payments_order_id`, `uq_payments_idempotency_key`, `uq_outbox_dedup_key`.
- `downgrade base` → `upgrade head` на **рабочей** базе: прошло, 7 таблиц вернулись,
  `alembic check` чистый. **Это разовая проверка на пустой базе; дальше downgrade
  на рабочей базе не делаем — только на тестовой.**
- `pytest -q` → 5 passed: round-trip миграций, CHECK на оверсейле, частичный индекс
  (вторая активная бронь падает, после `expired` проходит) + 2 прежних теста /health.
- Тесты проверены на «непустоту»: с временно снесёнными `ck_sales_not_oversold`
  и `uq_reservations_sale_id_user_id_active` оба теста падают с `DID NOT RAISE IntegrityError`.
- `ruff check .` — All checks passed; `ruff format --check .` — 26 files already formatted.
**Дальше:** резерв брони атомарным условным UPDATE и эндпоинты распродаж — начинать с теста
на конкурентные запросы за последнюю единицу.
