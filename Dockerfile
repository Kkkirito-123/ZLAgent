FROM python:3.11-slim-bookworm AS runtime

ARG PIP_INDEX_URL
ARG APT_MIRROR_URL

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=5 \
    ZLAGENT_HOST=0.0.0.0 \
    ZLAGENT_PORT=8020 \
    ZLAGENT_DATA_DIR=/app/data \
    ZLAGENT_CONFIG_DIR=/app/config \
    ZLAGENT_WORKSPACE_DIR=/app/workspace \
    ZLAGENT_PACKAGES_DIR=/app/.packages \
    NPM_CONFIG_PREFIX=/app/.packages/npm \
    NPM_CONFIG_IGNORE_SCRIPTS=true \
    NPM_CONFIG_CACHE=/app/.packages/npm-cache \
    UV_CACHE_DIR=/app/.packages/uv-cache \
    UV_TOOL_DIR=/app/.packages/uv/tools \
    UV_TOOL_BIN_DIR=/app/.packages/pip/bin \
    UV_PYTHON_INSTALL_DIR=/app/.packages/uv/python \
    PIP_USER=1 \
    PIP_CACHE_DIR=/app/.packages/pip-cache \
    PYTHONUSERBASE=/app/.packages/pip \
    PATH=/app/.packages/npm/bin:/app/.packages/pip/bin:/app/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin

WORKDIR /app

# v0.35 — Install Node.js LTS so the mcp-discovery skill can shell out
# to ``npx -y mcporter ...`` and to the broader npm-distributed MCP
# server ecosystem (``@modelcontextprotocol/server-*``, ``mcp-*``).
# v0.45.1 — bumped from Bookworm-default 18.x to NodeSource Node 20
# LTS so MCP packages declaring ``engines: { node: '20 || >=22' }``
# (e.g. @kevinwatt/yt-dlp-mcp, lru-cache v11, rimraf v6) stop printing
# EBADENGINE warnings. v0.45.1 also adds ffmpeg (used by yt-dlp to mux
# audio/video) and creates /app/Downloads so the yt-dlp MCP server
# passes its boot-time validateConfig() check.
# Layer placed BEFORE the Python deps + app code so a
# requirements.txt or backend/* change doesn't bust this cache.
#
# APT_MIRROR_URL build-arg follows the PIP_INDEX_URL pattern: pass it
# from a network where the default Debian mirrors are slow or
# unreachable (notably mainland China). Maps both deb.debian.org and
# security.debian.org to the supplied base URL. Examples:
#   docker compose build --build-arg APT_MIRROR_URL=https://mirrors.tuna.tsinghua.edu.cn
#   docker compose build --build-arg APT_MIRROR_URL=https://mirrors.aliyun.com
#   docker compose build --build-arg APT_MIRROR_URL=https://mirrors.ustc.edu.cn
# Bookworm uses the DEB822 /etc/apt/sources.list.d/debian.sources
# format; older /etc/apt/sources.list is patched too as a safety net.
RUN set -eux; \
    if [ -n "${APT_MIRROR_URL:-}" ]; then \
        MIRROR="${APT_MIRROR_URL%/}"; \
        for f in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; do \
            if [ -f "$f" ]; then \
                sed -i \
                    -e "s|https\\?://deb.debian.org|$MIRROR|g" \
                    -e "s|https\\?://security.debian.org|$MIRROR|g" \
                    "$f"; \
            fi; \
        done; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg ffmpeg passwd; \
    if command -v python3 >/dev/null 2>&1 && [ ! -x /usr/local/bin/python3.11 ]; then \
        ln -sf "$(command -v python3)" /usr/local/bin/python3.11; \
    fi; \
    mkdir -p /etc/apt/keyrings; \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg; \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends nodejs; \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./requirements.txt
RUN if [ -n "$PIP_INDEX_URL" ]; then \
        pip install --no-cache-dir --retries 5 --timeout 120 -i "$PIP_INDEX_URL" -r requirements.txt; \
    else \
        pip install --no-cache-dir --retries 5 --timeout 120 -r requirements.txt; \
    fi

# v1.1.0 — Pre-install ``uv`` (and therefore ``uvx``) into the system
# Python so the MCP install tool can manage Python-flavour MCP servers
# without an extra bootstrap roundtrip. Lives in /usr/local/bin which is
# already on PATH for every user. The ``uvx`` command resolves to ``uv
# tool run`` under the hood; both binaries land here.
RUN if [ -n "$PIP_INDEX_URL" ]; then \
        pip install --no-cache-dir --retries 5 --timeout 120 -i "$PIP_INDEX_URL" uv; \
    else \
        pip install --no-cache-dir --retries 5 --timeout 120 uv; \
    fi

RUN groupadd --gid 10001 zlagent \
    && useradd --uid 10001 --gid 10001 --home /app \
        --no-create-home --shell /usr/sbin/nologin zlagent \
    && mkdir -p /app/data /app/config /app/workspace /app/Downloads \
        /app/.packages/npm /app/.packages/npm-cache \
        /app/.packages/pip /app/.packages/pip/bin /app/.packages/pip-cache \
        /app/.packages/uv-cache /app/.packages/uv/tools /app/.packages/uv/python \
    && chown -R zlagent:zlagent /app/data /app/config /app/workspace /app/Downloads /app/.packages

# v1.1.0 — Keep build layers lean. MCP servers can be installed at
# runtime into the persisted /app/.packages volume via mcp_manage,
# which avoids a large and slow Docker build layer here.
USER root

COPY backend ./backend
COPY workspace ./workspace_seed
COPY docker-entrypoint.py ./docker-entrypoint.py
RUN chown -R zlagent:zlagent /app/backend /app/workspace_seed /app/docker-entrypoint.py \
    && chmod -R u=rwX,go=rX /app/backend /app/workspace_seed \
    && chmod 0555 /app/docker-entrypoint.py

USER zlagent

EXPOSE 8020

VOLUME ["/app/data", "/app/config", "/app/workspace", "/app/.packages"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"ZLAGENT_PORT\",\"8020\")}/api/health', timeout=5).read()" || exit 1

ENTRYPOINT ["python", "/app/docker-entrypoint.py"]
