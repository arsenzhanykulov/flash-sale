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
| PostgreSQL | `localhost:${POSTGRES_PORT}` | база данных |
| Mailpit — SMTP | `localhost:1025` | приём писем от backend |
| Mailpit — веб-UI | http://localhost:8025 | просмотр отправленных писем |

## Документация

- [docs/DATA_MODEL.md](docs/DATA_MODEL.md) — модель данных
- [docs/DECISIONS.md](docs/DECISIONS.md) — принятые решения (ADR)
- [docs/DEVLOG.md](docs/DEVLOG.md) — журнал разработки
- [CLAUDE.md](CLAUDE.md) — правила работы и инварианты
