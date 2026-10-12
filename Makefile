# Короткие обёртки над docker compose. Никакой скрытой логики: каждая цель —
# это ровно та команда, что написана ниже. Их же можно набирать руками,
# если make не установлен (см. README).

.PHONY: up down seed race load test lint logs

## up: собрать образы и поднять все сервисы, дождавшись готовности
# --wait: не возвращать управление, пока healthcheck-и не зелёные. Без него
# `make seed` сразу после `make up` может попасть в контейнер, где ещё идут
# миграции, и упасть на отсутствующих таблицах.
up:
	docker compose up --build -d --wait

## down: остановить сервисы (данные в томе pgdata остаются)
down:
	docker compose down

## seed: создать демо-данные — магазин, товар, покупателей, распродажу
seed:
	docker compose exec backend python -m app.scripts.seed

## race: два покупателя жмут «Купить» одновременно — увидеть гонку за единицу
race:
	docker compose exec backend python -m app.scripts.race

## load: нагрузочный прогон — 200 покупателей одновременно на 10 единиц
# Ненулевой код выхода при оверсейле, оставшихся held или расхождении писем.
load:
	docker compose exec backend python -m app.scripts.load

## test: тесты бэкенда на реальном Postgres и тесты платёжной заглушки
test:
	docker compose exec backend pytest -q
	docker compose exec payment-stub pytest -q

## lint: линт и проверка типов, бэкенд и фронтенд
lint:
	docker compose exec backend ruff check .
	docker compose exec backend ruff format --check .
	docker compose exec payment-stub ruff check .
	docker compose exec payment-stub ruff format --check .
	docker compose exec frontend npm run lint
	docker compose exec frontend npm run typecheck

## logs: логи всех сервисов, следить в реальном времени
logs:
	docker compose logs -f
