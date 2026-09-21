FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml requirements.txt README.md ./
COPY pycode ./pycode
COPY backend ./backend
COPY alembic.ini ./alembic.ini

RUN python -m pip install --no-cache-dir ".[backend]" \
    && useradd --create-home --uid 10001 pycode \
    && mkdir -p /var/lib/pycode/workspaces \
    && chown -R pycode:pycode /var/lib/pycode

USER pycode

CMD ["python", "-m", "uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
