#!/bin/sh
set -eu

set -- \
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

# The serve boundary owns all runner-lease validation (all-or-nothing group,
# value formats); this entrypoint only carries each declared value through.
for declared in \
  "--runner-lease-root=${ATELIER2_RUNNER_LEASE_ROOT:-}" \
  "--runner-image=${ATELIER2_RUNNER_IMAGE:-}" \
  "--runner-image-digest=${ATELIER2_RUNNER_IMAGE_DIGEST:-}" \
  "--runner-console-container=${ATELIER2_RUNNER_CONSOLE_CONTAINER:-}" \
  "--runner-core-identity-directory=${ATELIER2_RUNNER_CORE_IDENTITY_DIRECTORY:-}" \
  "--runner-accept-timeout-seconds=${ATELIER2_RUNNER_ACCEPT_TIMEOUT_SECONDS:-}"; do
  value="${declared#*=}"
  if [ -n "${value}" ]; then
    set -- "$@" "${declared%%=*}" "${value}"
  fi
done

exec atelier2 serve "$@"
