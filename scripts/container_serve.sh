#!/bin/sh
# In-image serve vector. Paths here are the container contract; the host
# start script only prepares mounts and builds.
set -eu

exec atelier2 serve \
  --database /var/lib/atelier2/store/atelier.sqlite \
  --effect-store /var/lib/atelier2/store/external.sqlite \
  --effect-adapter-revision loopback-v1 \
  --effect-destination local \
  --application-version atelier2-container \
  --source-commit "${ATELIER2_SOURCE_COMMIT:?source commit is required}" \
  --source-tree "${ATELIER2_SOURCE_TREE:?source tree is required}" \
  --frontend-dist /app/frontend/dist \
  --host 127.0.0.1 \
  --port 8422 \
  --agent-scratch-root /var/lib/atelier2/scratch \
  --claude-executable /usr/local/bin/claude \
  --claude-credential-directory /run/atelier2/claude
