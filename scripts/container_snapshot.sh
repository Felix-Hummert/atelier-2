#!/usr/bin/env bash
set -euo pipefail

if (($# != 2)); then
  echo "container snapshot: expected REPOSITORY and DESTINATION" >&2
  exit 2
fi

repository="$1"
destination="$2"
if [[ ! -d "${repository}" || -L "${repository}" ]]; then
  echo "container snapshot: repository is unavailable" >&2
  exit 1
fi
if [[ ! -d "${destination}" || -L "${destination}" ]] \
  || [[ -n "$(find "${destination}" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
  echo "container snapshot: destination must be an empty directory" >&2
  exit 1
fi
if ! status="$(git -C "${repository}" status --porcelain --untracked-files=all)"; then
  echo "container snapshot: source status is unavailable" >&2
  exit 1
fi
if [[ -n "${status}" ]]; then
  echo "container snapshot: source tree must be clean" >&2
  exit 1
fi
if ! source_commit="$(git -C "${repository}" rev-parse --verify 'HEAD^{commit}')"; then
  echo "container snapshot: source commit identity is missing" >&2
  exit 1
fi
if ! source_tree="$(git -C "${repository}" rev-parse --verify 'HEAD^{tree}')"; then
  echo "container snapshot: source tree identity is missing" >&2
  exit 1
fi
if [[ "${source_tree}" != "$(git -C "${repository}" rev-parse "${source_commit}^{tree}")" ]]; then
  echo "container snapshot: source tree does not belong to source commit" >&2
  exit 1
fi
git -C "${repository}" archive --format=tar "${source_commit}" \
  | tar -C "${destination}" -xf -
if [[ "${source_commit}" != "$(git -C "${repository}" rev-parse --verify 'HEAD^{commit}')" ]] \
  || [[ "${source_tree}" != "$(git -C "${repository}" rev-parse --verify 'HEAD^{tree}')" ]] \
  || [[ -n "$(git -C "${repository}" status --porcelain --untracked-files=all)" ]]; then
  echo "container snapshot: source changed while it was archived" >&2
  exit 1
fi

printf '%s %s\n' "${source_commit}" "${source_tree}"
