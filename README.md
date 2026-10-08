# Flash Sale

Сервис флэш-распродаж: магазин выставляет партию товара по спеццене на короткое время,
покупателей больше, чем товара. Главное требование — ни одна единица не может быть
продана дважды, даже когда десятки покупателей жмут «Купить» в одну секунду.

## Требования

Только Docker — ничего другого ставить локально не нужно: Python, Node, Postgres
и почтовый сервер живут в контейнерах.

- **Docker Engine 24+** (или Docker Desktop 4.x)
- **Docker Compose v2.20+** — нужны ключ `name:` верхнего уровня и флаг `--wait`

Проверялось на Docker 25.0.2 и Docker Compose v2.24.3 (macOS).
`make` не обязателен: рядом с каждой командой ниже указан её эквивалент на `docker compose`.

## Запуск

```bash
cp .env.example .env
make up      # docker compose up --build -d --wait
make seed    # docker compose exec backend python -m app.scripts.seed
```

Первая сборка занимает несколько минут. `make up` возвращает управление, только когда
сервисы прошли healthcheck, поэтому `make seed` можно запускать сразу следом.

Если первый шаг пропущен, `make up` скажет `env file ... /.env not found` — скопируйте
`.env.example` и повторите.

Миграции накатываются автоматически при старте контейнера `backend`. Если миграция
упадёт, контейнер не запустится — на старой схеме сервис работать не будет (ADR-016).

Остановить: `make down` (`docker compose down`). Данные остаются в томе `pgdata`.
Полный сброс вместе с базой — `docker compose down -v`.

## Адреса

| Что | Адрес |
|---|---|
| Витрина | http://localhost:5173 |
| Swagger (живая документация API) | http://localhost:8000/docs |
| Письма (Mailpit) | http://localhost:8025 |
| API | http://localhost:8000 |

Пароля нет (ADR-007): на экране входа достаточно email. `make seed` создаёт трёх
пользователей:

| Email | Роль |
|---|---|
| `shop@example.com` | магазин |
| `buyer1@example.com` | покупатель |
| `buyer2@example.com` | покупатель |

Любой другой email тоже подойдёт — неизвестный становится покупателем.

### Что показывает seed

Распродажа на 5 единиц, старт через 2 минуты, длительность 30 минут. Параметры можно
менять — удобно, чтобы посмотреть разные состояния витрины:

```bash
# распродажа уже идёт прямо сейчас
docker compose exec backend python -m app.scripts.seed --start-in 0 --duration 30

# стартует через минуту: видно таймер и как включается кнопка «Купить»
docker compose exec backend python -m app.scripts.seed --start-in 1 --duration 10
```

Повторный запуск безопасен: пользователи и товар переиспользуются, а распродажа
обновляется, если по ней ещё нет броней.

## Если порт занят

Нужны свободные **8000** (API) и **5173** (витрина), а также **1025** и **8025** (Mailpit).
Порт Postgres на хост не публикуется, поэтому локальный Postgres на 5432 запуску
не мешает (ADR-017).

Признак занятого порта в выводе `make up`:

```
Error ... failed to bind host port for 0.0.0.0:8000 ... address already in use
```

Кто держит порт:

```bash
lsof -nP -iTCP:8000 -sTCP:LISTEN     # macOS / Linux
```

Порты задаются в `.env` — поправьте и повторите `make up`:

```ini
BACKEND_PORT=8001
FRONTEND_PORT=5174
MAILPIT_SMTP_PORT=1026
MAILPIT_UI_PORT=8026
```

Меняя `BACKEND_PORT`, поправьте и две связанные переменные, иначе браузер будет
стучаться не туда, а бэкенд отклонит запрос по CORS:

```ini
VITE_API_URL=http://localhost:8001
CORS_ORIGINS=http://localhost:5174
```

### Доступ к базе внешним клиентом

По умолчанию порт Postgres не опубликован. Проще всего зайти изнутри:

```bash
docker compose exec postgres psql -U flash_sale -d flash_sale
```

Если нужен psql или GUI-клиент с хоста, включите публикацию порта:

```bash
cp docker-compose.override.example.yml docker-compose.override.yml
make up
```

`docker compose` подхватывает `docker-compose.override.yml` сам, без дополнительных
флагов. Файл в git не попадает. Порт берётся из `POSTGRES_PORT` в `.env` (по умолчанию 5433).

## Тесты и линт

```bash
make test    # docker compose exec backend pytest -q
make lint    # ruff + oxlint + tsc, см. ниже
```

`make lint` выполняет четыре команды:

```bash
docker compose exec backend ruff check .
docker compose exec backend ruff format --check .
docker compose exec frontend npm run lint        # oxlint
docker compose exec frontend npm run typecheck   # tsc
```

Линтер фронтенда — **oxlint** (конфиг `frontend/.oxlintrc.json`), он идёт
в современном шаблоне Vite вместо ESLint. ESLint в проекте не используется.

Тесты бэкенда идут на **реальном Postgres**, в отдельной базе `flash_sale_test`:
она пересоздаётся перед каждым прогоном, схема накатывается миграциями, после прогона
база удаляется. Рабочая база не затрагивается — имя тестовой обязано заканчиваться
на `_test` и отличаться от базы из `DATABASE_URL`, иначе тесты падают, не выполнив
ни одного запроса (ADR-011).

Логи: `make logs` (`docker compose logs -f`), одного сервиса — `docker compose logs -f backend`.

## Структура

```
backend/     FastAPI: api/routes + api/schemas → services → models, миграции Alembic
frontend/    Vite + React + TypeScript; все запросы через единственный модуль src/api
docs/        решения (ADR), модель данных, журнал разработки, лог промптов
docker-compose.yml   postgres, mailpit, backend, frontend
Makefile     короткие обёртки над docker compose
.env.example шаблон окружения, копируется в .env
```

## Состояние проекта

День 1 закрыт. Готово:

- инфраструктура: Postgres 16, Mailpit, backend, frontend — всё в `docker compose`
- схема БД со всеми инвариантами: `CHECK (sold + held <= quantity)` как последняя линия
  защиты от оверсейла, частичный уникальный индекс «одна активная бронь на покупателя»
- вход по email без пароля, JWT
- чтение распродаж: список и страница, остаток и статус `upcoming/active/ended`
  считает Postgres, а не Python
- витрина: вход, список, страница распродажи, таймер по серверному времени
- 33 теста на реальном Postgres

Ещё не сделано — дни 2–3:

- **резерв брони** — атомарный условный `UPDATE` и `POST /api/sales/{id}/reserve`;
  кнопка «Купить» пока только объясняет, что резерв впереди
- **оплата** через сервис-заглушку `payment-stub` (режимы approve / decline / hang)
  и сверка зависших платежей
- **письма** через transactional outbox — ровно одно письмо на событие
- **live-обновления остатка** по WebSocket через Postgres LISTEN/NOTIFY
- **фоновые задачи**: свипер просроченных броней, закрытие распродаж

## Документация

- [docs/DECISIONS.md](docs/DECISIONS.md) — принятые решения с альтернативами (ADR)
- [docs/DATA_MODEL.md](docs/DATA_MODEL.md) — модель данных и ограничения в схеме
- [docs/DEVLOG.md](docs/DEVLOG.md) — журнал разработки по шагам
- [docs/PROMPTS.md](docs/PROMPTS.md) — лог промптов к агенту
- [CLAUDE.md](CLAUDE.md) — правила работы и инварианты, которые нельзя нарушать
