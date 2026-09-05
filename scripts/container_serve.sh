#!/bin/sh
set -eu

exec atelier2 serve \
  --database /var/lib/atelier2/store/atelier.sqlite \
  --effect-store /var/lib/atelier2/store/external.sqlite \
  --effect-adapter-revision loopback-v1 \
  --effect-destination local \
  --application-version "${ATELIER2_SOURCE_COMMIT:?source commit is required}" \
  --source-commit "${ATELIER2_SOURCE_COMMIT:?source commit is required}" \
  --source-tree "${ATELIER2_SOURCE_TREE:?source tree is required}" \
  --frontend-dist /app/frontend/dist \
  --host 0.0.0.0 \
  --port 8422
