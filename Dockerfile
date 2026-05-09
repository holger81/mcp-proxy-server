# MCP Proxy — runtime image (Python deps + app + static admin assets).
FROM python:3.12-slim-bookworm

# GitHub release tag for https://github.com/tecnologicachile/mail-mcp (Linux amd64 binary only).
ARG MAIL_MCP_VERSION=v0.4.5

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    MCP_PROXY_STATIC_ROOT=/app/static \
    MAIL_MCP_VERSION=${MAIL_MCP_VERSION}

WORKDIR /app

RUN pip install --no-cache-dir --upgrade pip "uv~=0.5.11"

COPY pyproject.toml README.md ./
COPY src ./src
COPY static ./static
COPY servers/mcp-news-server ./servers/mcp-news-server
COPY docker ./docker

RUN uv pip install --system . \
    && uv pip install --system ./servers/mcp-news-server

# Debian's nodejs/npm are too old for many MCP packages. NodeSource 24.x matches packages that declare engines.node >=24 (e.g. @codefuturist/email-mcp).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        gosu \
        tzdata \
        xz-utils \
    && curl -fsSL https://deb.nodesource.com/setup_24.x -o /tmp/nodesource_setup.sh \
    && bash /tmp/nodesource_setup.sh \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -f /tmp/nodesource_setup.sh \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app

# mail-mcp (Rust): prebuilt x86_64-unknown-linux-gnu only. Omit on non-amd64 builds (no upstream asset).
# MAIL_MCP_VERSION may be a tag (e.g. v0.4.5) or "latest" → GitHub's rolling latest release asset URL.
RUN mkdir -p /opt/mail-mcp \
    && ARCH="$(dpkg --print-architecture)" \
    && if [ "$ARCH" = "amd64" ]; then \
      ASSET="mail-mcp-x86_64-unknown-linux-gnu.tar.xz" \
      && if [ "$MAIL_MCP_VERSION" = "latest" ]; then \
        URL="https://github.com/tecnologicachile/mail-mcp/releases/latest/download/${ASSET}"; \
      else \
        URL="https://github.com/tecnologicachile/mail-mcp/releases/download/${MAIL_MCP_VERSION}/${ASSET}"; \
      fi \
      && curl -fsSL "$URL" | tar -xJf - -C /tmp \
      && install -m 0755 "/tmp/mail-mcp-x86_64-unknown-linux-gnu/mail-mcp" /opt/mail-mcp/mail-mcp \
      && rm -rf "/tmp/mail-mcp-x86_64-unknown-linux-gnu"; \
    else \
      echo "mail-mcp: no GitHub Linux binary for ${ARCH} (upstream ships amd64 only). Use linux/amd64 image or install manually under /data/mail-mcp/." \
        > /opt/mail-mcp/README.txt; \
    fi

COPY docker/mail-mcp-runner.sh /usr/local/bin/mail-mcp
RUN chmod +x /usr/local/bin/mail-mcp /app/docker/install-mail-mcp-release.sh

COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh \
    && chmod +x /app/docker/seed_mcp_news.py

# Bundled default RSS list for first-time /data volume (copied by seed script).
COPY servers/mcp-news-server/src/mcp_news_server/default_feeds.yaml /app/mcp-news-default-feeds.yaml

EXPOSE 8080

VOLUME ["/data"]

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["uvicorn", "mcp_proxy.app:app", "--host", "0.0.0.0", "--port", "8080"]
