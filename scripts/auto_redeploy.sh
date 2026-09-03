#!/usr/bin/env bash
set -euo pipefail

# Runs one serialized poll-and-maybe-redeploy cycle. This watcher owns the
# decision to deploy; serve_live_update.sh owns the complete loopback update.

readonly deploy_remote="origin"
readonly deploy_branch="main"
readonly health_url="http://127.0.0.1:8422/atelier/api/v1/health"
readonly runs_url="http://127.0.0.1:8422/atelier/api/v1/runs"
readonly log_tag="atelier2-autodeploy"
readonly failure_alert_threshold=3
readonly busy_alert_threshold=30
readonly alert_repeat_seconds=3600
readonly check_run_lookup_timeout_seconds=60
readonly check_run_appearance_grace_seconds=1800
readonly github_repository="repos/FlexOr2/atelier-2"
# Must match serve_live_update.sh's own intake_refused_exit_code: the deploy
# still succeeded (the new commit is served), only a workflow intake was
# refused, so this is not an ordinary failure tick.
readonly intake_refused_exit_code=3

repository="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Git replaces a tracked file by unlink-then-create, so a shell that already
# opened scripts/serve_live_update.sh keeps reading the OLD bytes for the
# rest of its run — a checkout that fast-forwards under a running deploy
# would silently execute stale deploy logic. staged_serve_live_update holds
# the path of the target commit's own script once materialised (see below
# for where and why), so the logic that becomes live is the logic that
# runs; cleanup_staged_serve_live_update removes it on every exit path.
staged_serve_live_update=""
cleanup_staged_serve_live_update() {
  # An `&&`-guarded rm would make this trap's own exit status the falsy
  # short-circuit result when nothing is staged, and bash keeps that as the
  # script's exit code — always resolve the `if` so cleanup never rewrites
  # the tick's real outcome.
  if [[ -n "${staged_serve_live_update}" ]]; then
    rm -f -- "${staged_serve_live_update}"
  fi
}
trap cleanup_staged_serve_live_update EXIT

log() {
  local priority="$1"
  shift
  logger -t "${log_tag}" -p "user.${priority}" -- "$*"
}

log_debug() {
  log debug "$*"
}

log_warning() {
  log warning "$*"
}

log_error() {
  log err "$*"
}

log_info() {
  log info "$*"
}

if ! git_admin_directory="$(git -C "${repository}" rev-parse --absolute-git-dir 2>&1)"; then
  log_error "cannot resolve the Git admin directory: ${git_admin_directory}"
  exit 1
fi
readonly git_admin_directory
readonly failure_count_file="${git_admin_directory}/auto-redeploy.failures"
readonly busy_count_file="${git_admin_directory}/auto-redeploy.busy"
readonly last_alert_file="${git_admin_directory}/auto-redeploy.last-alert"
readonly lock_file="${git_admin_directory}/auto-redeploy.lock"

read_number() {
  local file="$1"
  local value
  if [[ ! -f "${file}" ]]; then
    printf '0'
    return
  fi
  if ! value="$(<"${file}")" || ! [[ "${value}" =~ ^[0-9]+$ ]]; then
    log_warning "state file ${file} is unreadable or corrupt; resetting it"
    printf '0' >"${file}"
    printf '0'
    return
  fi
  printf '%s' "${value}"
}

reset_counters() {
  printf '0' >"${failure_count_file}"
  printf '0' >"${busy_count_file}"
  rm -f "${last_alert_file}"
}

now_seconds() {
  date +%s
}

failure_escalation_due() {
  local failure_count="$1"
  local now last_alert
  ((failure_count >= failure_alert_threshold)) || return 1
  now="$(now_seconds)"
  [[ "${now}" =~ ^[0-9]+$ ]] || return 0
  last_alert="$(read_number "${last_alert_file}")"
  if ((failure_count != failure_alert_threshold && now - last_alert < alert_repeat_seconds)); then
    return 1
  fi
  printf '%s' "${now}" >"${last_alert_file}"
}

record_failure() {
  local reason="$1"
  local failure_count
  failure_count="$(read_number "${failure_count_file}")"
  failure_count=$((failure_count + 1))
  printf '%s' "${failure_count}" >"${failure_count_file}"
  log_debug "failure count now ${failure_count} (this tick: ${reason})"
  failure_escalation_due "${failure_count}" || return 1
  log_error "ALERT: ${failure_count} ticks in a row failed, reason: ${reason}; auto-redeploy needs operator attention"
}

fail_tick() {
  local reason="$1"
  if record_failure "${reason}"; then
    exit 1
  fi
  exit 0
}

refuse_tick() {
  local reason="$1"
  local message="$2"
  log_warning "${message}"
  fail_tick "${reason}"
}

record_busy_deferral() {
  local active_run_count="$1"
  local busy_count
  busy_count="$(read_number "${busy_count_file}")"
  busy_count=$((busy_count + 1))
  printf '%s' "${busy_count}" >"${busy_count_file}"
  log_debug "busy count now ${busy_count} (${active_run_count} active runs)"
  if ((busy_count == busy_alert_threshold)); then
    log_warning "deploy deferred on ${busy_count} ticks in a row; ${active_run_count} runs are still active"
  fi
}

served_status=""
served_commit=""
read_served_health() {
  local body parsed
  body="$(curl -fsS --max-time 5 "${health_url}")" || return 1
  parsed="$(python3 -c '
import json
import re
import sys

try:
    payload = json.load(sys.stdin)
    status = payload["status"]
    commit = payload["source_commit"]
except (json.JSONDecodeError, KeyError, TypeError):
    raise SystemExit(1)
if not isinstance(status, str) or not re.fullmatch(r"[a-z_]+", status):
    raise SystemExit(1)
if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
    raise SystemExit(1)
print(f"{status}\t{commit}")
' <<<"${body}")" || return 1
  IFS=$'\t' read -r served_status served_commit <<<"${parsed}"
}

active_run_count() {
  local state body items_count
  local total=0
  for state in STARTED WAITING_INPUT WAITING_RECONCILIATION; do
    if ! body="$(curl -fsS --max-time 5 "${runs_url}?state=${state}&limit=1" 2>&1)"; then
      printf 'cannot read %s runs: %s' "${state}" "${body}"
      return 1
    fi
    if ! items_count="$(python3 -c '
import json
import sys

try:
    payload = json.load(sys.stdin)
    items = payload["items"]
except (json.JSONDecodeError, KeyError, TypeError):
    raise SystemExit(1)
if not isinstance(items, list):
    raise SystemExit(1)
print(len(items))
' <<<"${body}" 2>/dev/null)"; then
      printf 'cannot parse %s runs' "${state}"
      return 1
    fi
    total=$((total + items_count))
  done
  printf '%s' "${total}"
}

remote_check_status() {
  local commit_sha="$1"
  local commit_timestamp="$2"
  local check_runs lookup_exit
  if ! command -v gh >/dev/null 2>&1; then
    printf 'GitHub CLI (gh) is unavailable on PATH'
    return 1
  fi
  if check_runs="$(timeout "${check_run_lookup_timeout_seconds}" gh api --paginate \
    "${github_repository}/commits/${commit_sha}/check-runs" \
    --jq 'if (type == "object" and (.total_count | type == "number") and (.check_runs | type == "array")) then "envelope\t\(.total_count)", (.check_runs[] | "check\t\(.status // "")\t\(.conclusion // "")") else error("GitHub returned malformed check-runs response") end' 2>&1)"; then
    :
  else
    lookup_exit=$?
    if ((lookup_exit == 124)); then
      printf 'check-run lookup timed out'
    else
      printf '%s' "${check_runs}"
    fi
    return 1
  fi

  local record status conclusion
  local expected_check_count=""
  local observed_check_count=0
  local has_envelope=false
  local has_running_check=false
  local has_failed_check=false
  while IFS=$'\t' read -r record status conclusion; do
    case "${record}" in
      envelope)
        if [[ ! "${status}" =~ ^[0-9]+$ || -n "${conclusion}" ]]; then
          printf 'GitHub returned a malformed check-runs envelope'
          return 1
        fi
        if [[ -n "${expected_check_count}" && "${expected_check_count}" != "${status}" ]]; then
          printf 'GitHub returned inconsistent paginated check-run counts'
          return 1
        fi
        expected_check_count="${status}"
        has_envelope=true
        ;;
      check)
        observed_check_count=$((observed_check_count + 1))
        case "${status}" in
          queued|in_progress)
            has_running_check=true
            ;;
          completed)
            case "${conclusion}" in
              success|skipped|neutral)
                ;;
              failure|cancelled|timed_out)
                has_failed_check=true
                ;;
              *)
                printf 'GitHub returned an unknown completed check-run conclusion'
                return 1
                ;;
            esac
            ;;
          *)
            printf 'GitHub returned an unknown check-run status'
            return 1
            ;;
        esac
        ;;
      *)
        printf 'GitHub returned a malformed check-runs response'
        return 1
        ;;
    esac
  done <<<"${check_runs}"

  if [[ "${has_envelope}" != true || "${observed_check_count}" != "${expected_check_count}" ]]; then
    printf 'GitHub returned an incomplete check-runs response'
    return 1
  fi
  if ((observed_check_count == 0)); then
    local now
    now="$(now_seconds)" || return 1
    [[ "${now}" =~ ^[0-9]+$ ]] || return 1
    if ((now - commit_timestamp >= check_run_appearance_grace_seconds)); then
      printf 'failed (GitHub has not reported a check run within %ss)' "${check_run_appearance_grace_seconds}"
    else
      printf 'waiting (GitHub has not reported a check run yet)'
    fi
    return
  fi
  if [[ "${has_running_check}" == true ]]; then
    printf 'waiting (one or more GitHub check runs are still running)'
  elif [[ "${has_failed_check}" == true ]]; then
    printf 'failed (one or more GitHub check runs are red)'
  else
    printf 'green'
  fi
}

exec 200>"${lock_file}"
if ! flock -n 200; then
  log_debug "another auto-redeploy tick is already running"
  exit 0
fi

if ! fetch_output="$(git -C "${repository}" fetch --quiet "${deploy_remote}" "${deploy_branch}" 2>&1)"; then
  refuse_tick "git fetch failed" "cannot fetch ${deploy_remote}/${deploy_branch}: ${fetch_output}"
fi
if ! target_commit="$(git -C "${repository}" rev-parse --verify FETCH_HEAD 2>&1)"; then
  refuse_tick "fetched commit is unreadable" "cannot resolve the fetched ${deploy_remote}/${deploy_branch}: ${target_commit}"
fi

if ! read_served_health; then
  refuse_tick "served health is unreadable" "served health is unavailable or malformed; refusing to deploy without a known current state"
fi
if [[ "${served_status}" != "serving" ]]; then
  refuse_tick "served health is not serving" "served health reports ${served_status@Q}, not serving"
fi

if [[ "${served_commit}" == "${target_commit}" ]]; then
  reset_counters
  log_debug "already current at ${target_commit}; nothing to deploy"
  exit 0
fi

if ! current_branch="$(git -C "${repository}" symbolic-ref --quiet --short HEAD 2>&1)"; then
  refuse_tick "checkout is detached" "deploy checkout is not on a branch; leaving it untouched"
fi
if [[ "${current_branch}" != "${deploy_branch}" ]]; then
  refuse_tick "checkout is not on ${deploy_branch}" "deploy checkout is on ${current_branch@Q}, not ${deploy_branch}; leaving it untouched"
fi
if ! worktree_status="$(git -C "${repository}" status --porcelain --untracked-files=all 2>&1)"; then
  refuse_tick "checkout status is unreadable" "deploy checkout status is unreadable: ${worktree_status}"
fi
if [[ -n "${worktree_status}" ]]; then
  refuse_tick "checkout is dirty" "deploy checkout is dirty; leaving operator work untouched"
fi

if ! active_runs="$(active_run_count)"; then
  refuse_tick "run state is unreadable before checking GitHub checks" "cannot establish whether runs are active before checking GitHub checks: ${active_runs}"
fi
if ((active_runs > 0)); then
  record_busy_deferral "${active_runs}"
  exit 0
fi

if ! commit_timestamp="$(git -C "${repository}" show -s --format=%ct "${target_commit}" 2>&1)"; then
  refuse_tick "commit time is unreadable" "cannot read the fetched commit time: ${commit_timestamp}"
fi
if ! [[ "${commit_timestamp}" =~ ^[0-9]+$ ]]; then
  refuse_tick "commit time is invalid" "fetched commit has invalid commit time ${commit_timestamp@Q}"
fi
if ! check_status="$(remote_check_status "${target_commit}" "${commit_timestamp}")"; then
  refuse_tick "GitHub check status is unreadable" "cannot establish check-run status for ${target_commit}: ${check_status}"
fi
case "${check_status}" in
  green)
    ;;
  waiting\ *)
    log_debug "checks for ${target_commit} are still waiting: ${check_status}"
    exit 0
    ;;
  failed\ *)
    refuse_tick "GitHub checks are red" "checks for ${target_commit} are not green: ${check_status}"
    ;;
  *)
    refuse_tick "GitHub check status is unknown" "unexpected check-run classification for ${target_commit}: ${check_status}"
    ;;
esac

if ! active_runs="$(active_run_count)"; then
  refuse_tick "run state is unreadable before update" "cannot establish whether runs are active immediately before update: ${active_runs}"
fi
if ((active_runs > 0)); then
  record_busy_deferral "${active_runs}"
  exit 0
fi

# Staged at <git-dir>/serve_live_update.sh (not scripts/, not /tmp): the
# repository's git directory is one level below the repository root, so the
# target script's own self-location (dirname "$BASH_SOURCE"/..) still
# resolves to this checkout, while nothing under .git/ ever appears in
# `git status` — a real deploy's clean-checkout preflight cannot be tripped
# by the staged file, and a killed tick leaves no dirt behind for the next
# one to trip on either.
staged_serve_live_update="${git_admin_directory}/serve_live_update.sh"
if ! show_error="$(git -C "${repository}" show "${target_commit}:scripts/serve_live_update.sh" 2>&1 >"${staged_serve_live_update}")"; then
  refuse_tick "target deploy script could not be read" "cannot read scripts/serve_live_update.sh from ${target_commit}: ${show_error}"
fi
chmod +x "${staged_serve_live_update}"

if "${staged_serve_live_update}" "${target_commit}"; then
  reset_counters
  log_info "auto redeploy: main now served at ${target_commit}"
  exit 0
else
  update_exit=$?
fi
if ((update_exit == intake_refused_exit_code)); then
  reset_counters
  log_warning "main now served at ${target_commit}; workflow intake refused (see serve_live_update journal)"
  exit 0
fi
refuse_tick "loopback update failed" "serve_live_update.sh failed for ${target_commit} (exit ${update_exit})"
