# Журнал разработки


---

## <!-- Thu Oct  8 23:59:31 +06 2026 
--> — Старт работы
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

## 2026-10-09 03:06 — Seed, упрощённый вход и API чтения
**Ветка:** `feat/read-api`
**Сделано:** seed-скрипт `python -m app.scripts.seed` с аргументами `--quantity/--start-in/--duration`;
`POST /api/auth/login` и `GET /api/auth/me`, зависимость текущего пользователя;
`GET /api/time`; `GET /api/sales` и `GET /api/sales/{id}`. Новые настройки `JWT_SECRET`,
`JWT_ALGORITHM`, `ACCESS_TOKEN_TTL_MINUTES`. Тестов стало 33 (было 5).
**Зависимости:** `pyjwt` 2.15.1 — подпись токенов (для HS256 не тянет `cryptography`,
проверка `exp` встроена, живая поддержка); `pydantic[email]` → `email-validator` 2.3.0 —
мусор в теле запроса на вход отсекается 422, а не превращается в пользователя.
**Решения:** ADR-013 (PyJWT, HS256, без состояния на сервере), ADR-014 (статус и остаток
считает Postgres).
- Вход — один `INSERT ... ON CONFLICT (email) DO UPDATE SET email = excluded.email RETURNING`:
  без «SELECT → INSERT», который проигрывает гонку при двух одновременных входах.
  Обновляется только email, роль существующего пользователя не трогается.
- `HTTPBearer(auto_error=False)`: со включённым автоответом отсутствие заголовка
  даёт 403, а правильный код для «не аутентифицирован» — 401.
- Seed переиспользует только распродажу своего seed-товара и только без броней;
  иначе создаёт новую — обнулять `sold`/`held` у распродажи с бронями нельзя (ADR-002).
  Правило описано в README.
- `JWT_SECRET` в `.env.example` с dev-значением, чтобы `cp .env.example .env` давал
  рабочий запуск; дефолта в коде нет — без секрета приложение не стартует.
- Пакет `api` разделён на `api/routes` (только эндпоинты) и `api/schemas`
  (форма запросов и ответов), по модулю на домен в каждом — как в `models/`
  и `services/`. `deps.py` и `router.py` остались на уровне `api/`: первый общий
  для всех роутов, второй — единственное место, где объявлены префиксы.
**Агент:** Claude назвал библиотеку и обосновал выбор до начала работы. Поправил я:
(1) dev-значение `JWT_SECRET` в шаблоне при отсутствии дефолта в коде;
(2) добавить тест, что вход `shop@example.com` не меняет роль на `buyer`;
(3) seed переиспользует распродажу только своего товара, а не любую без броней,
и правило должно быть в README. Claude по ходу заметил, что фикстуры для API-тестов
обязаны коммитить: запросы идут через отдельную сессию и незакоммиченных строк не видят.
**Проверка:**
- `pytest -q` → **33 passed**: границы статуса (за секунду до старта, ровно в момент старта,
  секунду после конца, закрытая досрочно), остаток `quantity=5, sold=2, held=1 → 2`,
  остаток 0 при распродаже под ноль, 404 на неизвестный id, 422 на невалидный UUID,
  вход создаёт покупателя и не дублирует его (включая другой регистр email),
  вход магазина сохраняет роль, `/me` с токеном / без / с мусором / с просроченным,
  422 на невалидный email, `/api/time` совпадает с `select now()`, seed идемпотентен.
- Вживую: seed дважды → `users=3 products=1 sales=1`, вторая распродажа «обновлена».
  `--start-in 0` → `active`, `--start-in -10 --duration 5` → `ended`, `--quantity 0` → отказ.
- `POST /api/auth/login` с `  BUYER1@Example.COM  ` даёт тот же id, что и `buyer1@example.com`.
  Вход `shop@example.com` → роль `shop`.
- `/api/auth/me`: с токеном 200, без токена 401, с мусором 401. `/docs` → 200.
- Проверено, что без `JWT_SECRET` настройки падают `ValidationError` (запуск контейнера
  с `.env` без этой переменной).
- `ruff check .` — All checks passed; `ruff format --check .` — 40 files already formatted.
**Дальше:** резерв брони атомарным условным UPDATE — начинать с теста на конкурентные
запросы за последнюю единицу.

## 2026-10-09 03:45 — Скелет frontend и витрина
**Ветка:** `feat/frontend-skeleton`
**Сделано:** `frontend/` на Vite 8 + React 19 + TypeScript 6, отдельный сервис в compose
(порт 5173, hot reload через том). Экран входа с хранением токена и выходом; список
распродаж и страница распродажи — товар, цена, остаток, статус, таймер. Кнопка «Купить»
блокируется, пока статус не `active`. Новые переменные `FRONTEND_PORT`, `VITE_API_URL`.
**Зависимости:** `react`, `react-dom`, `react-router-dom` (нужны настоящие URL: ссылку
на распродажу можно переслать, работает кнопка «назад»); dev — `vite`,
`@vitejs/plugin-react`, `typescript`, типы, `oxlint`. Шаблон Vite теперь идёт с **oxlint**
вместо ESLint — взял дефолт шаблона, это одна зависимость вместо пяти. Намеренно не брал:
`axios` (хватает `fetch`), `@tanstack/react-query` (несоразмерно трём эндпоинтам),
`date-fns`/`dayjs` (арифметика таймера — вычитание чисел), UI-библиотеки.
**Решения:** ADR-015 — фронт и бэк общаются по сети, адрес API только из `VITE_API_URL`,
прокси Vite нет, статика из FastAPI не раздаётся. Токен в `localStorage`.
- Все запросы — через единственный модуль `src/api`; вызовов `fetch` в остальном коде нет.
- `VITE_API_URL` хостовой (`http://localhost:8000`), не `http://backend:8000`: запрос делает
  браузер, а он имени сервиса compose не разрешает.
- Node 24 (действующий LTS, поддержка до 2028-04) — тег проверен, не взят по памяти.
- Анонимный том на `/app/node_modules`: иначе монтирование папки с хоста поверх `/app`
  скрывает зависимости из образа и dev-сервер не стартует.
- `usePolling` в watcher Vite: события файловой системы с macOS до контейнера не доходят.
**Агент:** Claude спросил про роутер и поведение кнопки до начала работы. Сам нашёл
и исправил четыре замечания линтера, а не заглушил их:
(1) «граница таймера пройдена» теперь **выводится при рендере** из серверного статуса
и серверных часов, а не хранится в состоянии — отдельного состояния с сбросом в эффекте
больше нет, и опрос сервера прекращается сам, когда статус сменился;
(2) в `useSale` состояние загрузки тоже выводится при рендере (сравнением id), без сброса
в эффекте — иначе на миг показывалась бы чужая распродажа;
(3) хуки `useAuth` и `useServerClock` вынесены из файлов с компонентами в отдельные модули —
файл, экспортирующий и компонент, и хук, ломает hot reload;
(4) параметр-свойство в конструкторе `ApiError` заменено явным полем: `erasableSyntaxOnly`
в tsconfig такой синтаксис запрещает. Также перенёс `frontend/.gitignore` из шаблона —
без него oxlint линтил `node_modules` (3672 ошибки на чужом коде).
**Проверка:**
- `docker compose up --build` — четыре сервиса подняты, Vite 8.3.4 отвечает на 5173.
- `VITE_API_URL` действительно подставляется: в отданном браузеру модуле
  `import.meta.env = {... "VITE_API_URL": "http://localhost:8000"}`.
- Модули приложения отдаются: `/src/main.tsx`, `/src/App.tsx`, `/src/api/index.ts`,
  `/src/sales/SalePage.tsx`, `/src/index.css` — все 200.
- **CORS с origin фронта:** `GET /api/time` и `/api/sales` возвращают
  `access-control-allow-origin: http://localhost:5173`; preflight `OPTIONS` для
  `POST /api/auth/login` → 200 с `access-control-allow-headers: content-type`;
  сам POST отдаёт токен. С посторонним origin разрешающего заголовка нет.
- Переход границы на стороне API: на `21:44:09` при старте `21:44:10` ещё `upcoming`,
  на `21:44:12` — `active`.
- Hot reload через том: правка `src/index.css` → в логах `[vite] hmr update /src/index.css`.
- `npm run typecheck` чисто, `npm run lint` — **0 warnings, 0 errors**,
  `npm run build` собирается (269 kB, 86 kB gzip).
- Бэкенд не пострадал: `pytest -q` → 33 passed, `ruff check`/`format --check` чисто.
- **Не проверено мной:** визуальный осмотр в браузере и вкладка Network — расширение
  Chrome не установлено. Сетевую часть проверил запросами с заголовком `Origin`.
**Дальше:** резерв брони атомарным условным UPDATE — начинать с теста на конкурентные
запросы за последнюю единицу; затем кнопка «Купить» подключается к `POST /reserve`.

## 2026-10-09 04:04 — Закрытие дня 1: запуск одной командой
**Ветка:** `chore/day1-wrapup`
**Сделано:** миграции применяются автоматически в entrypoint контейнера backend;
`Makefile` с целями `up/down/seed/test/lint/logs`; README переписан целиком под
ревьюера на чистой машине; порт Postgres на хост больше не публикуется, вместо него
`docker-compose.override.example.yml`; healthcheck у фронтенда.
**Решения:** ADR-016 (миграции в entrypoint, `set -e` + `exec` — падение миграции
не даёт сервису подняться), ADR-017 (порт Postgres не публикуется по умолчанию).
- `make up` = `docker compose up --build -d --wait`. Флаг `--wait` принципиален:
  без него `make seed` сразу после `make up` мог попасть в контейнер, где ещё идут
  миграции, и упасть на отсутствующих таблицах. Чтобы `--wait` говорил правду и про
  фронт, у него появился healthcheck — раньше его не было.
- `make down` без `-v`: данные не уничтожаем. Полный сброс описан в README отдельно.
- В Makefile нет скрытой логики: каждая цель — ровно та команда `docker compose`,
  что написана рядом. В README она указана рядом с каждой целью — у ревьюера
  может не быть `make`.
**Агент:** Claude предложил не публиковать порт Postgres, обнаружив, что это
единственная вещь, ломающая запуск по инструкции на машине с локальным Postgres.
Поправил я: (1) публикацию порта включать через `docker-compose.override.yml`
(сам файл в `.gitignore`, в репозитории — `.example`), локально создать override
с 5433; (2) в README рядом с каждой `make`-командой написать исходную
`docker compose`; (3) проверить, какой линтер реально стоит во фронте — в плане
я упоминал eslint, а в проекте **oxlint 1.87.0** с `.oxlintrc.json`, ESLint нет
вообще (шаблон Vite перешёл на oxlint). В README и Makefile теперь написан oxlint.
**Проверка с нуля, как у ревьюера:**
- `docker compose down -v`, удалены локальные `.env` и `docker-compose.override.yml`,
  том `pgdata` снесён. Порт 5432 на машине при этом **занят контейнером другого
  проекта** — именно тот случай, который раньше ломал запуск.
- Дальше строго три шага README и ничего больше: `cp .env.example .env` → `make up`
  → `make seed`. Все четыре сервиса поднялись `healthy`, миграции накатились сами
  (`[entrypoint] Применяем миграции` → `Running upgrade -> 223134f5b6cc`), seed создал
  данные. **Ни одного действия вне README не потребовалось.**
- Адреса из README: витрина 5173, Swagger 8000/docs, Mailpit 8025, `/api/sales`,
  `/health` — все 200. Три тестовых логина из README работают, роли `shop`/`buyer`/`buyer`.
- `docker compose ps` подтверждает, что у postgres порт на хост не опубликован (`5432/tcp`).
- **Падение миграции проверено подсунутой битой ревизией:** контейнер ушёл в цикл
  перезапусков (restarts 6→8), `/health` ни разу не отдал 200, ревизия в базе осталась
  `223134f5b6cc` — на старой схеме сервис не работал. После удаления пробы — `healthy`.
- Забытый первый шаг даёт понятное `env file ... /.env not found`; указание на это
  добавлено в README.
- `make test` → 33 passed. `make lint` → ruff clean, 46 files formatted,
  oxlint 0 warnings / 0 errors, tsc чисто.

---

## Итоги дня 1

**Сделано за день — пять шагов:**
1. Инфраструктура: Postgres 16 и Mailpit в compose, healthcheck-и, именованный том.
2. Скелет backend: FastAPI, слои `api → services → models`, `/health` с реальной
   проверкой БД, тесты на настоящем Postgres в отдельной пересоздаваемой базе.
3. Схема БД: модели всех 7 таблиц и первая миграция Alembic со всеми инвариантами —
   `CHECK (sold + held <= quantity)`, частичный уникальный индекс по активным броням.
4. Seed, вход по email с JWT, API чтения распродаж со статусом и остатком из SQL.
5. Фронтенд: витрина, вход, страница распродажи, таймер по серверному времени.

Плюс это закрытие: запуск сводится к трём строкам README.

**Ключевые решения дня:** резерв только атомарным условным UPDATE и CHECK как последняя
линия защиты (ADR-002); тесты на реальном Postgres в отдельной базе с защитой от сноса
рабочей (ADR-011); схема в тестах накатывается миграциями, а не `create_all` (ADR-012);
статус и остаток считает Postgres, границы совпадают с условием резерва (ADR-014);
фронт и бэк общаются по сети, без прокси и раздачи статики (ADR-015); миграции
в entrypoint (ADR-016); порт Postgres не публикуется (ADR-017).

## 2026-10-09 HH:MM +06 — Пауза. Конец дня 1
**Состояние:** main зелёный, проект запускается с нуля по README
(cp .env.example .env → make up → make seed). Готово: инфраструктура,
схема БД с инвариантами, вход, API чтения, витрина с серверным таймером.
**Не сделано (день 2):** резерв брони атомарным UPDATE, свипер истёкших
броней, live-обновления остатков по WebSocket, тесты на гонки.
**При возобновлении:** прочитать CLAUDE.md и эту запись, ветка
feat/reservations.