#!/usr/bin/env bash
# Build and start the packaged serve. Redeploy is a deliberate rerun.
# This script does not start, stop, or replace atelier2-live.service.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export ATELIER2_STATE="${ATELIER2_STATE:-${XDG_STATE_HOME:-$HOME/.local/state}/atelier2}"
export ATELIER2_UID="${ATELIER2_UID:-$(id -u)}"
export ATELIER2_GID="${ATELIER2_GID:-$(id -g)}"

if ! ATELIER2_SOURCE_COMMIT="$(git -C "${REPO}" rev-parse --verify "HEAD^{commit}" 2>/dev/null)"; then
  echo "container up: source commit identity is missing" >&2
  exit 1
fi
if [[ -z "${ATELIER2_SOURCE_COMMIT}" || "${ATELIER2_SOURCE_COMMIT}" == "unknown" ]]; then
  echo "container up: source commit identity is unknown" >&2
  exit 1
fi
if ! git -C "${REPO}" cat-file -e "${ATELIER2_SOURCE_COMMIT}^{commit}" 2>/dev/null; then
  echo "container up: source commit identity is unknown" >&2
  exit 1
fi
if ! ATELIER2_SOURCE_TREE="$(git -C "${REPO}" rev-parse --verify "HEAD^{tree}" 2>/dev/null)"; then
  echo "container up: source tree identity is missing" >&2
  exit 1
fi
if [[ -z "${ATELIER2_SOURCE_TREE}" || "${ATELIER2_SOURCE_TREE}" == "unknown" ]]; then
  echo "container up: source tree identity is unknown" >&2
  exit 1
fi
if ! git -C "${REPO}" cat-file -e "${ATELIER2_SOURCE_TREE}^{tree}" 2>/dev/null; then
  echo "container up: source tree identity is unknown" >&2
  exit 1
fi
if ! EXPECTED_SOURCE_TREE="$(git -C "${REPO}" rev-parse --verify "${ATELIER2_SOURCE_COMMIT}^{tree}" 2>/dev/null)"; then
  echo "container up: source tree identity is missing" >&2
  exit 1
fi
if [[ "${ATELIER2_SOURCE_TREE}" != "${EXPECTED_SOURCE_TREE}" ]]; then
  echo "container up: source tree does not belong to source commit" >&2
  exit 1
fi
export ATELIER2_SOURCE_COMMIT
export ATELIER2_SOURCE_TREE

if [[ -z "${ATELIER2_CLAUDE_CREDENTIALS:-}" ]]; then
  echo "container up: set ATELIER2_CLAUDE_CREDENTIALS to the host path of .credentials.json" >&2
  exit 1
fi
if [[ ! -f "${ATELIER2_CLAUDE_CREDENTIALS}" || -L "${ATELIER2_CLAUDE_CREDENTIALS}" ]]; then
  echo "container up: ATELIER2_CLAUDE_CREDENTIALS must be a regular file, not a directory or symlink" >&2
  exit 1
fi
if [[ "$(basename "${ATELIER2_CLAUDE_CREDENTIALS}")" != ".credentials.json" ]]; then
  echo "container up: ATELIER2_CLAUDE_CREDENTIALS must be a file named .credentials.json" >&2
  exit 1
fi

if command -v systemctl >/dev/null 2>&1; then
  if systemctl --user is-active --quiet atelier2-live.service 2>/dev/null; then
    echo "container up: atelier2-live.service is still active." >&2
    echo "  this script will not stop it. Container start is documented, not a live cutover." >&2
    exit 1
  fi
fi

umask 077
mkdir -p "${ATELIER2_STATE}/store" "${ATELIER2_STATE}/scratch"
chmod 0700 "${ATELIER2_STATE}" "${ATELIER2_STATE}/store" "${ATELIER2_STATE}/scratch"

for path in "${ATELIER2_STATE}" "${ATELIER2_STATE}/store" "${ATELIER2_STATE}/scratch"; do
  mode="$(stat -c '%a' "${path}")"
  if [[ "${mode}" != "700" ]]; then
    echo "container up: ${path} must be mode 0700, is ${mode}" >&2
    exit 1
  fi
done

# Candidate bytes come from the named commit, never from dirty or untracked
# worktree files that would otherwise sit in compose's context directory.
CANDIDATE="$(mktemp -d "${TMPDIR:-/tmp}/atelier2-candidate.XXXXXX")"
cleanup_candidate() {
  cd "${REPO}"
  rm -rf "${CANDIDATE}"
}
trap cleanup_candidate EXIT
git -C "${REPO}" archive --format=tar "${ATELIER2_SOURCE_COMMIT}" | tar -C "${CANDIDATE}" -xf -
cd "${CANDIDATE}"
docker compose build
docker compose up -d
cd "${REPO}"

echo "container up: cockpit -> http://127.0.0.1:8422/atelier/"
echo "  logs: (cd ${REPO} && docker compose logs -f)"
echo "  live unit atelier2-live.service was not touched. Redeploy is a rerun of this script."
