FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md requirements.txt LICENSE ./
COPY src/ ./src/

RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 app

USER app

ENTRYPOINT ["mcp-obsidian"]
