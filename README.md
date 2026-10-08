# Flash Sale

Сервис флэш-распродаж: магазин выставляет партию товара по спеццене на короткое время,
покупателей больше, чем товара, и ни одна единица не должна быть продана дважды.

## Требования

- Docker (Desktop или Engine) с Docker Compose v2 — больше ничего локально ставить не нужно.

## Запуск

```bash
cp .env.example .env    # при необходимости поправить порты
docker compose up --build
```

Порт `5432` на хосте часто занят локальным Postgres — в этом случае задать свободный
через `POSTGRES_PORT` в `.env`. Внутри docker-сети порт всегда `5432`,
`DATABASE_URL` править не нужно.

## Сервисы

| Сервис | Адрес | Назначение |
|---|---|---|
| Backend | http://localhost:8000 | API (`/health`) |
| Swagger | http://localhost:8000/docs | живая документация API |
| PostgreSQL | `localhost:${POSTGRES_PORT}` | база данных |
| Mailpit — SMTP | `localhost:1025` | приём писем от backend |
| Mailpit — веб-UI | http://localhost:8025 | просмотр отправленных писем |

## Разработка

```bash
docker compose exec backend alembic upgrade head   # миграции
docker compose exec backend alembic check          # схема БД и модели совпадают?
docker compose exec backend pytest -q              # тесты
docker compose exec backend ruff check .           # линт
docker compose exec backend ruff format .          # форматирование
```

Новая миграция: `docker compose exec backend alembic revision --autogenerate -m "<msg>"`.
Существующие миграции не редактируем.

Код backend смонтирован в контейнер, `uvicorn --reload` подхватывает правки без пересборки.

Тесты идут на реальном Postgres в отдельной базе `flash_sale_test` (пересоздаётся
перед каждым прогоном и удаляется после). Рабочая база не затрагивается: имя тестовой
обязано заканчиваться на `_test` и отличаться от базы из `DATABASE_URL`, иначе тесты
падают, не выполнив ни одного запроса. Адрес можно переопределить через `TEST_DATABASE_URL`.

## Документация

- [docs/DATA_MODEL.md](docs/DATA_MODEL.md) — модель данных
- [docs/DECISIONS.md](docs/DECISIONS.md) — принятые решения (ADR)
- [docs/DEVLOG.md](docs/DEVLOG.md) — журнал разработки
- [CLAUDE.md](CLAUDE.md) — правила работы и инварианты
