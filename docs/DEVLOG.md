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
