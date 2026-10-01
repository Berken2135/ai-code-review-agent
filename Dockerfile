FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# README.md is part of the package metadata: the build fails without it.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

COPY alembic.ini ./
COPY alembic ./alembic

RUN useradd --create-home appuser
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://localhost:8000/health')"

# Apply migrations, then serve.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn reviewer.main:create_app --factory --host 0.0.0.0 --port 8000"]
