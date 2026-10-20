FROM python:3.12-slim AS base

# TZ влияет только на читаемость логов: бизнес-логика берёт пояс из
# period.timezone и от системного не зависит. Но разбирать ночной прогон
# проще, когда лог и снимок в одних сутках.
ENV TZ=Europe/Moscow \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /usr/local/bin/uv

WORKDIR /app

# Слой зависимостей отдельно от кода: правка сервиса не пересобирает venv.
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-install-project --no-dev

COPY app ./app
COPY config ./config
COPY migrations ./migrations
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 10001 app && chown -R app:app /app /opt/venv
USER app

HEALTHCHECK --interval=10m --timeout=20s --start-period=12h --retries=2 \
    CMD python -m app health || exit 1

CMD ["python", "-m", "app", "scheduler"]
