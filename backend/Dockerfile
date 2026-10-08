# Образ uv приносит и Python 3.12, и сам uv — отдельная установка не нужна.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    # Ставим в системный префикс: uvicorn, pytest и ruff сразу в PATH, без `uv run`.
    UV_PROJECT_ENVIRONMENT=/usr/local \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Зависимости отдельным слоем: правка кода не вызывает пересборку окружения.
# Dev-группа (pytest, ruff) нужна внутри контейнера — тесты гоняются там.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen

COPY app ./app
COPY tests ./tests

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
