#!/usr/bin/env bash
set -euo pipefail

# Runs one poll-and-maybe-redeploy cycle: if origin/main has advanced past
# what the local live serve currently reports, it fast-forwards this checkout
# and hands the new commit to `container_live.sh update`, which owns the
# atomic redeploy (store migration, health gate, and self-restart of the
# previous container on any failure -- see scripts/container_live.sh). This
# script adds nothing to that atomicity; it only decides *whether* to call it,
# and refuses to touch anything when it cannot safely tell.

readonly deploy_remote="origin" deploy_branch="main"
readonly health_url="http://127.0.0.1:8422/atelier/api/v1/health"

fail() {
  echo "auto redeploy: $1" >&2
  exit 1
}

repository="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
container_live="${repository}/scripts/container_live.sh"

served_status=""
served_commit=""
# Reads the served health endpoint, the same contract compose.yaml's own
# healthcheck verifies. Sets served_status/served_commit and returns 1,
# leaving nothing to trust, when the endpoint is unreachable or malformed --
# curl's own connect/read timeout bounds the poll's worst case.
read_served_health() {
  local body
  body="$(curl -fsS --max-time 5 "${health_url}")" || return 1
  # A malformed body (no match) makes the grep|sed pipeline itself fail under
  # `pipefail`; `|| true` lets that reach the emptiness check below as the
  # intended "malformed" outcome instead of aborting the script right here.
  served_status="$(grep -oE '"status"[[:space:]]*:[[:space:]]*"[a-z]+"' <<<"${body}" \
    | head -n 1 | sed -E 's/^.*"([a-z]+)"$/\1/')" || true
  served_commit="$(grep -oE '"source_commit"[[:space:]]*:[[:space:]]*"[0-9a-f]{40}"' <<<"${body}" \
    | head -n 1 | sed -E 's/^.*"([0-9a-f]{40})"$/\1/')" || true
  [[ -n "${served_status}" && -n "${served_commit}" ]] || return 1
}

git -C "${repository}" fetch --quiet "${deploy_remote}" "${deploy_branch}"
target_commit="$(git -C "${repository}" rev-parse --verify FETCH_HEAD)"

read_served_health \
  || fail "the served health check is unavailable; refusing to redeploy without a known current state"
[[ "${served_status}" == "serving" ]] \
  || fail "the served health check reports status ${served_status@Q}, not serving; refusing to redeploy over an unhealthy serve"

if [[ "${served_commit}" == "${target_commit}" ]]; then
  echo "auto redeploy: already current at ${target_commit}"
  exit 0
fi

current_branch="$(git -C "${repository}" rev-parse --abbrev-ref HEAD)"
[[ "${current_branch}" == "${deploy_branch}" ]] \
  || fail "the deploy checkout is on ${current_branch@Q}, not ${deploy_branch}"
worktree_status="$(git -C "${repository}" status --porcelain --untracked-files=all)" \
  || fail "the deploy checkout status is unavailable"
[[ -z "${worktree_status}" ]] \
  || fail "the deploy checkout is not clean; refusing to pull over local changes"

git -C "${repository}" pull --ff-only --quiet "${deploy_remote}" "${deploy_branch}" \
  || fail "the deploy checkout could not fast-forward to ${target_commit}"
[[ "$(git -C "${repository}" rev-parse --verify HEAD)" == "${target_commit}" ]] \
  || fail "the deploy checkout did not land on the fetched commit ${target_commit}"

if ! update_output="$("${container_live}" update 2>&1)"; then
  echo "${update_output}" >&2
  fail "container_live.sh update refused ${target_commit}; the previously served commit is expected to still be running"
fi
echo "${update_output}"

read_served_health \
  || fail "the served health check is unavailable after update; verify with: ${container_live} status"
[[ "${served_status}" == "serving" && "${served_commit}" == "${target_commit}" ]] \
  || fail "the served commit did not advance to ${target_commit} after update; verify with: ${container_live} status"

echo "auto redeploy: main now served at ${target_commit}"
