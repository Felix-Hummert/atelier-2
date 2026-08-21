FROM node:22.22.0-bookworm-slim AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build


FROM python:3.12.13-slim-trixie AS runtime
COPY --from=ghcr.io/astral-sh/uv:0.10.9 /uv /uvx /bin/
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON=3.12.13 \
    UV_PYTHON_PREFERENCE=only-system

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
RUN python -c "import sqlite3; assert sqlite3.connect(':memory:').execute(\"SELECT unixepoch('subsec') * 1000\").fetchone()[0] is not None"

COPY src/ ./src/
COPY --from=frontend /build/frontend/dist ./frontend/dist
RUN uv sync --locked --no-dev \
    && rm -f /bin/uv /bin/uvx

ARG ATELIER2_SOURCE_COMMIT
ARG ATELIER2_SOURCE_TREE
RUN test -n "${ATELIER2_SOURCE_COMMIT}" \
    && test -n "${ATELIER2_SOURCE_TREE}" \
    && groupadd --gid 10001 atelier2 \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin atelier2 \
    && install --directory --owner atelier2 --group atelier2 --mode 0700 /var/lib/atelier2/store
LABEL atelier2.source.commit=${ATELIER2_SOURCE_COMMIT} \
      atelier2.source.tree=${ATELIER2_SOURCE_TREE}

COPY scripts/container_serve.sh /app/container_serve.sh
RUN chmod 0755 /app/container_serve.sh

USER atelier2
ENV PATH=/app/.venv/bin:/usr/local/bin:/usr/bin:/bin \
    ATELIER2_SOURCE_COMMIT=${ATELIER2_SOURCE_COMMIT} \
    ATELIER2_SOURCE_TREE=${ATELIER2_SOURCE_TREE}
ENTRYPOINT ["/app/container_serve.sh"]
