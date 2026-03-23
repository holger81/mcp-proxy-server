# MCP Proxy — runtime image (Python deps + app + static admin assets).
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    MCP_PROXY_STATIC_ROOT=/app/static

WORKDIR /app

RUN pip install --no-cache-dir --upgrade pip "uv~=0.5.11"

COPY pyproject.toml README.md ./
COPY src ./src
COPY static ./static

RUN uv pip install --system .

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8080

VOLUME ["/data"]

CMD ["uvicorn", "mcp_proxy.app:app", "--host", "0.0.0.0", "--port", "8080"]
