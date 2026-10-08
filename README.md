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
| Frontend | http://localhost:5173 | витрина |
| Backend | http://localhost:8000 | API (`/health`, `/api/...`) |
| Swagger | http://localhost:8000/docs | живая документация API |
| PostgreSQL | `localhost:${POSTGRES_PORT}` | база данных |
| Mailpit — SMTP | `localhost:1025` | приём писем от backend |
| Mailpit — веб-UI | http://localhost:8025 | просмотр отправленных писем |

## Демо-данные

```bash
docker compose exec backend python -m app.scripts.seed
docker compose exec backend python -m app.scripts.seed --quantity 5 --start-in 2 --duration 30
```

Создаёт магазин `shop@example.com`, товар, покупателей `buyer1@example.com`
и `buyer2@example.com` и распродажу. Аргументы: `--quantity` (по умолчанию 5),
`--start-in` — через сколько минут старт (2; отрицательное значение — распродажа
уже идёт), `--duration` — длительность в минутах (30). Окно считается от `now()`
в БД, а не от часов машины.

**Повторный запуск не ломается и не плодит сущности.** Правило переиспользования:

- пользователи и товар находятся по постоянным email и названию;
- распродажа переиспользуется только **своя** — созданная по seed-товару — и только
  если по ней **ещё нет броней**: тогда обновляются окно и количество, счётчики там нулевые.
  Если брони есть, создаётся новая распродажа: обнулять `sold`/`held` у распродажи
  с бронями нельзя, счётчики разъехались бы со строками в `reservations` (ADR-002).
  Чужие распродажи seed не трогает.

## Вход

Пароля нет (ADR-007): `POST /api/auth/login` с email отдаёт JWT, неизвестный email
становится покупателем. Токен передаётся как `Authorization: Bearer <token>`,
текущего пользователя показывает `GET /api/auth/me`.

```bash
curl -s -X POST http://localhost:8000/api/auth/login \
  -H 'Content-Type: application/json' -d '{"email":"buyer1@example.com"}'
```

`JWT_SECRET` в `.env.example` — значение для локальной разработки, в продакшене
меняется (`openssl rand -hex 32`). Дефолта в коде нет: без секрета приложение не стартует.

## Разработка

### Фронтенд

Отдельный сервис: фронт и бэк общаются по сети, прокси Vite не используется
и статика из FastAPI не раздаётся (ADR-015). Адрес API берётся только из
`VITE_API_URL` — он **хостовой** (`http://localhost:8000`), а не `http://backend:8000`:
запрос делает браузер, а он имени сервиса compose не знает. Этот адрес должен быть
перечислен в `CORS_ORIGINS` на бэкенде.

Все запросы идут через единственный модуль `frontend/src/api` — вызовов `fetch`
в остальном коде нет.

Таймер считается по серверному времени: при загрузке один `GET /api/time`,
дальше тикаем локально по измеренному смещению. Когда срок истёк, статус
распродажи перезапрашивается у сервера — кнопка «Купить» никогда не включается
по часам браузера.

```bash
docker compose exec frontend npm run lint        # oxlint
docker compose exec frontend npm run typecheck   # tsc
docker compose exec frontend npm run build       # продакшен-сборка
```

### Бэкенд

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
