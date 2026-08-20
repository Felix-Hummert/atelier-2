# Packaged serve: bake the locked uv project and the built cockpit, then run
# unprivileged. Redeploy is an image rebuild. Credentials never enter the image.
#
# The process binds 127.0.0.1. Compose therefore uses the host network: a
# bridge-published port would force 0.0.0.0 inside the container, which the
# billed-provider loopback rule refuses.

FROM node:22.22.0-bookworm-slim AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build


FROM debian:bookworm-slim AS claude-cli
ARG CLAUDE_VERSION=2.1.233
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --user-group atelier2
USER atelier2
ENV HOME=/home/atelier2
RUN curl -fsSL https://claude.ai/install.sh | bash -s -- "${CLAUDE_VERSION}"


FROM python:3.12.3-slim-bookworm AS runtime
COPY --from=ghcr.io/astral-sh/uv:0.10.9 /uv /uvx /bin/
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON=3.12.3 \
    UV_PYTHON_PREFERENCE=only-system

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project

COPY src/ ./src/
COPY --from=frontend /build/frontend/dist ./frontend/dist
RUN uv sync --locked --no-dev \
    && rm -f /bin/uv /bin/uvx

COPY --from=claude-cli /home/atelier2/.local/bin/claude /usr/local/bin/claude
RUN chmod 0755 /usr/local/bin/claude

ARG ATELIER2_UID=1000
ARG ATELIER2_GID=1000
ARG ATELIER2_SOURCE_COMMIT
ARG ATELIER2_SOURCE_TREE
RUN if [ -z "${ATELIER2_SOURCE_COMMIT}" ] || [ "${ATELIER2_SOURCE_COMMIT}" = "unknown" ] \
    || [ -z "${ATELIER2_SOURCE_TREE}" ] || [ "${ATELIER2_SOURCE_TREE}" = "unknown" ]; then \
      echo "image recipe: source commit and source tree identity is missing or unknown" >&2; \
      exit 1; \
    fi
LABEL ATELIER2_SOURCE_COMMIT=${ATELIER2_SOURCE_COMMIT} \
      ATELIER2_SOURCE_TREE=${ATELIER2_SOURCE_TREE}
RUN if ! getent group "${ATELIER2_GID}" >/dev/null; then \
      groupadd --gid "${ATELIER2_GID}" atelier2; \
    fi \
    && useradd --create-home --uid "${ATELIER2_UID}" --gid "${ATELIER2_GID}" atelier2 \
    && mkdir -p /var/lib/atelier2/store /var/lib/atelier2/scratch /run/atelier2/claude \
    && : > /run/atelier2/claude/.credentials.json \
    && chmod 0700 /var/lib/atelier2 /var/lib/atelier2/store /var/lib/atelier2/scratch \
                  /run/atelier2 /run/atelier2/claude \
    && chmod 0600 /run/atelier2/claude/.credentials.json \
    && chown -R "${ATELIER2_UID}:${ATELIER2_GID}" /home/atelier2 /app /var/lib/atelier2 /run/atelier2

COPY scripts/container_serve.sh /app/container_serve.sh
RUN chmod 0755 /app/container_serve.sh

USER atelier2
ENV HOME=/home/atelier2 \
    PATH=/app/.venv/bin:/usr/local/bin:/usr/bin:/bin \
    ATELIER2_SOURCE_COMMIT=${ATELIER2_SOURCE_COMMIT} \
    ATELIER2_SOURCE_TREE=${ATELIER2_SOURCE_TREE}

ENTRYPOINT ["/app/container_serve.sh"]
