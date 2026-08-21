#!/usr/bin/env bash
set -euo pipefail

repository="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
for variable in ATELIER2_DEPLOYMENT ATELIER2_PUBLISHED_PORT ATELIER2_RESTART_POLICY; do
  if [[ -n "${!variable+x}" ]]; then
    echo "container up: ambient container mode is forbidden" >&2
    exit 1
  fi
done
export ATELIER2_DEPLOYMENT="disposable"
export ATELIER2_PUBLISHED_PORT="0"
export ATELIER2_RESTART_POLICY="no"

lifecycle=""
snapshot=""
descriptor=""
project=""
docker_started=0
handoff=0
cleanup_running=0

print_teardown_command() {
  printf 'ATELIER2_SOURCE_COMMIT=%q ATELIER2_SOURCE_TREE=%q ' \
    "${ATELIER2_SOURCE_COMMIT}" "${ATELIER2_SOURCE_TREE}"
  printf '%q ' docker compose --project-name "${project}" -f "${descriptor}" \
    down --volumes --rmi local --remove-orphans
  printf '&& rm -f -- %q && rmdir -- %q\n' "${descriptor}" "${lifecycle}"
}
cleanup() {
  local original_status="$?"
  local final_status="${original_status}"
  if ((cleanup_running)); then
    exit "${original_status}"
  fi
  cleanup_running=1
  trap - EXIT
  trap '' HUP INT TERM

  if [[ -n "${snapshot}" ]] && ! rm -rf -- "${snapshot}"; then
    echo "container up: build snapshot cleanup failed" >&2
    if ((original_status == 0)); then
      final_status=1
    fi
  fi

  if ((handoff && original_status == 0)); then
    :
  elif ((docker_started)); then
    if ! docker compose --project-name "${project}" -f "${descriptor}" down \
      --volumes --rmi local --remove-orphans; then
      printf 'container up: cleanup failed; run: ' >&2
      print_teardown_command >&2
    elif ! rm -f -- "${descriptor}"; then
      printf 'container up: lifecycle descriptor cleanup failed; run: ' >&2
      print_teardown_command >&2
      if ((original_status == 0)); then
        final_status=1
      fi
    fi
  elif [[ -n "${descriptor}" ]] && ! rm -f -- "${descriptor}"; then
    echo "container up: lifecycle descriptor cleanup failed" >&2
    if ((original_status == 0)); then
      final_status=1
    fi
  fi

  if [[ -n "${lifecycle}" && ! -e "${descriptor}" ]] && ! rmdir "${lifecycle}"; then
    echo "container up: lifecycle directory cleanup failed" >&2
    if ((original_status == 0)); then
      final_status=1
    fi
  fi
  exit "${final_status}"
}
lifecycle="$(mktemp -d "${TMPDIR:-/tmp}/atelier2-lifecycle.XXXXXX")"
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

snapshot="$(mktemp -d "${lifecycle}/snapshot.XXXXXX")"
descriptor="${lifecycle}/teardown.compose.yaml"
project="atelier2-$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')"
compose=(docker compose --project-name "${project}" --project-directory "${snapshot}" -f "${snapshot}/compose.yaml")
if ! read -r ATELIER2_SOURCE_COMMIT ATELIER2_SOURCE_TREE \
  < <("${repository}/scripts/container_snapshot.sh" "${repository}" "${snapshot}"); then
  exit 1
fi
export ATELIER2_SOURCE_COMMIT ATELIER2_SOURCE_TREE
{
  printf 'name: %s\n' "${project}"
  cat <<'YAML'
services:
  serve:
    build: .
    volumes:
      - type: volume
        source: store
        target: /var/lib/atelier2/store
        volume:
          nocopy: false
    networks:
      - serve
    labels: &source_labels
      atelier2.deployment: disposable
      atelier2.source.commit: ${ATELIER2_SOURCE_COMMIT:?source commit identity is missing}
      atelier2.source.tree: ${ATELIER2_SOURCE_TREE:?source tree identity is missing}
volumes:
  store:
    labels: *source_labels
networks:
  serve:
    labels: *source_labels
YAML
} >"${descriptor}"
chmod 0600 "${descriptor}"
docker_started=1
"${compose[@]}" build
"${compose[@]}" up --detach --wait --wait-timeout 30 --no-build
address="$("${compose[@]}" port serve 8422)"
if [[ "${address}" =~ ^127\.0\.0\.1:([0-9]{1,5})$ ]] \
  && ((10#${BASH_REMATCH[1]} >= 1 && 10#${BASH_REMATCH[1]} <= 65535)); then
  port="${BASH_REMATCH[1]}"
else
  echo "container up: Docker returned an invalid loopback port" >&2
  exit 1
fi
handoff=1

echo "container up: cockpit -> http://127.0.0.1:${port}/atelier/"
printf 'container up: stop -> '
print_teardown_command
