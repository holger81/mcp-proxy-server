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

# Debian's nodejs/npm are too old for many MCP packages (e.g. engines >=20). Use NodeSource 20.x LTS.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        gosu \
        tzdata \
    && curl -fsSL https://deb.nodesource.com/setup_20.x -o /tmp/nodesource_setup.sh \
    && bash /tmp/nodesource_setup.sh \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -f /tmp/nodesource_setup.sh \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app

COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

EXPOSE 8080

VOLUME ["/data"]

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["uvicorn", "mcp_proxy.app:app", "--host", "0.0.0.0", "--port", "8080"]
