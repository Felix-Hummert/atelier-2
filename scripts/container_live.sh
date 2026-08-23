#!/usr/bin/env bash
set -euo pipefail

readonly deployment="local-live" published_port="8422" restart_policy="unless-stopped"
readonly record_version="2" record_size_limit="16384" descriptor_size_limit="16384"
readonly project_name_prefix="atelier2-live-"

fail() {
  echo "container live: $1" >&2
  exit 1
}

if (($# < 1)); then
  fail "expected exactly one command: install, status, stop, start, uninstall, or update"
fi
command="$1"
fresh=0
case "${command}" in
  install | status | stop | start | uninstall)
    (($# == 1)) || fail "${command} takes no arguments"
    ;;
  update)
    if (($# == 1)); then
      :
    elif (($# == 2)) && [[ "$2" == "--fresh" ]]; then
      fresh=1
    else
      fail "update accepts an optional --fresh flag only"
    fi
    ;;
  *) fail "unknown command ${command}" ;;
esac

if [[ -n "${XDG_STATE_HOME:-}" ]]; then
  state_home="${XDG_STATE_HOME}"
elif [[ -n "${HOME:-}" ]]; then
  state_home="${HOME}/.local/state"
else
  fail "XDG state home is unavailable"
fi
if [[ "${state_home}" != /* || "${state_home}" == *$'\n'* ]]; then
  fail "XDG state home must be an absolute one-line path"
fi

repository="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
installation_directory="${state_home}/atelier2/container-live"
lock_file="${installation_directory}/lifecycle.lock"
record_file="${installation_directory}/installation.state"
descriptor_file="${installation_directory}/compose.yaml"
current_user_id="$(id -u)"
declare -A record=()
temporary_record=""
temporary_descriptor=""
snapshot_root=""
install_in_progress=0
installation_completed=0
uninstalled_existing_installation=0
uninstalled_existing_store=0
owned_project_has_volume=0
local_live_container_removed=0
local_live_network_removed=0
local_live_volume_removed=0
local_live_image_removed=0
update_old_container_id=""
update_old_image_id=""
update_old_stopped=0
update_completed=0

validate_private_directory() {
  local path="$1"
  [[ -d "${path}" && ! -L "${path}" ]] || return 1
  [[ "$(stat -c '%u:%a' -- "${path}")" == "${current_user_id}:700" ]]
}

validate_private_file() {
  local path="$1"
  local size_limit="$2"
  [[ -f "${path}" && ! -L "${path}" ]] || return 1
  [[ "$(stat -c '%u:%a' -- "${path}")" == "${current_user_id}:600" ]] || return 1
  (($(stat -c '%s' -- "${path}") <= size_limit))
}

prepare_installation_directory() {
  umask 077
  mkdir -p -- "${state_home}/atelier2"
  if [[ ! -e "${installation_directory}" ]]; then
    mkdir --mode=0700 -- "${installation_directory}"
  fi
  validate_private_directory "${installation_directory}" \
    || fail "installation directory must be private, owned, and free of symlinks"
  if [[ ! -e "${lock_file}" ]]; then
    (set -o noclobber; : >"${lock_file}") 2>/dev/null \
      || fail "lifecycle lock could not be created"
    chmod 0600 -- "${lock_file}"
  fi
  validate_private_file "${lock_file}" 0 \
    || fail "lifecycle lock must be a private empty regular file"
}
acquire_existing_lock() {
  validate_private_directory "${installation_directory}" \
    || fail "installation directory drifted"
  validate_private_file "${lock_file}" 0 || fail "lifecycle lock drifted"
  exec 9<>"${lock_file}"
  flock --nonblock 9 || fail "lifecycle is busy"
}
acquire_install_lock() {
  prepare_installation_directory
  acquire_existing_lock
}
record_value_is_valid() {
  local key="$1"
  local value="$2"
  case "${key}" in
    record_version) [[ "${value}" == "${record_version}" ]] ;;
    state) [[ "${value}" == "INSTALLING" || "${value}" == "INSTALLED" ]] ;;
    deployment) [[ "${value}" == "${deployment}" ]] ;;
    published_port) [[ "${value}" == "${published_port}" ]] ;;
    restart_policy) [[ "${value}" == "${restart_policy}" ]] ;;
    source_commit | source_tree | store_source_commit | store_source_tree) [[ "${value}" =~ ^[0-9a-f]{40}$ ]] ;;
    project) [[ "${value}" =~ ^atelier2-live-[0-9a-f]{16}$ ]] ;;
    descriptor_sha256 | configuration_sha256) [[ "${value}" =~ ^[0-9a-f]{64}$ ]] ;;
    engine_id) [[ "${value}" =~ ^[A-Za-z0-9:._-]{1,160}$ ]] ;;
    image_id) [[ "${value}" =~ ^sha256:[0-9a-f]{64}$ ]] ;;
    container_id | network_id) [[ "${value}" =~ ^[0-9a-f]{64}$ ]] ;;
    volume_name) [[ "${value}" =~ ^atelier2-live-[0-9a-f]{16}_store$ ]] ;;
    network_name) [[ "${value}" =~ ^atelier2-live-[0-9a-f]{16}_serve$ ]] ;;
    *) return 1 ;;
  esac
}
record_is_valid() {
  local key required=(record_version state deployment published_port restart_policy source_commit source_tree project descriptor_sha256 engine_id)
  for key in "${!record[@]}"; do
    record_value_is_valid "${key}" "${record[${key}]}" || return 1
  done
  if [[ "${record[state]:-}" == "INSTALLED" ]]; then
    required+=(
      image_id container_id volume_name network_name network_id
      configuration_sha256 store_source_commit store_source_tree
    )
  fi
  for key in "${required[@]}"; do
    [[ -n "${record[${key}]:-}" ]] || return 1
  done
  ((${#record[@]} == ${#required[@]})) || return 1
  [[ "${record[volume_name]:-${record[project]}_store}" == "${record[project]}_store" ]] \
    || return 1
  [[ "${record[network_name]:-${record[project]}_serve}" == "${record[project]}_serve" ]] \
    || return 1
}
read_record() {
  record=()
  validate_private_file "${record_file}" "${record_size_limit}" || return 1
  local key value
  while IFS='=' read -r key value || [[ -n "${key}${value}" ]]; do
    [[ "${key}" =~ ^[a-z0-9_]+$ && "${value}" != *$'\n'* ]] || return 1
    [[ -z "${record[${key}]+present}" ]] || return 1
    record["${key}"]="${value}"
  done <"${record_file}"
  record_is_valid
}
publish_record() {
  record_is_valid || fail "installation record identity is invalid"
  temporary_record="$(mktemp "${installation_directory}/.installation.XXXXXX")"
  chmod 0600 -- "${temporary_record}"
  {
    printf 'record_version=%s\n' "${record_version}"
    printf 'state=%s\n' "${record[state]}"
    printf 'deployment=%s\n' "${deployment}"
    printf 'published_port=%s\n' "${published_port}"
    printf 'restart_policy=%s\n' "${restart_policy}"
    printf 'source_commit=%s\n' "${record[source_commit]}"
    printf 'source_tree=%s\n' "${record[source_tree]}"
    printf 'project=%s\n' "${record[project]}"
    printf 'descriptor_sha256=%s\n' "${record[descriptor_sha256]}"
    printf 'engine_id=%s\n' "${record[engine_id]}"
    if [[ "${record[state]}" == "INSTALLED" ]]; then
      printf 'image_id=%s\n' "${record[image_id]}"
      printf 'container_id=%s\n' "${record[container_id]}"
      printf 'volume_name=%s\n' "${record[volume_name]}"
      printf 'network_name=%s\n' "${record[network_name]}"
      printf 'network_id=%s\n' "${record[network_id]}"
      printf 'configuration_sha256=%s\n' "${record[configuration_sha256]}"
      printf 'store_source_commit=%s\n' "${record[store_source_commit]}"
      printf 'store_source_tree=%s\n' "${record[store_source_tree]}"
    fi
  } >"${temporary_record}"
  sync -f "${temporary_record}"
  mv -f -- "${temporary_record}" "${record_file}"
  temporary_record=""
  sync -f "${installation_directory}"
}
descriptor_is_exact() {
  validate_private_file "${descriptor_file}" "${descriptor_size_limit}" \
    && [[ "$(sha256sum -- "${descriptor_file}" | cut -d ' ' -f 1)" == "${record[descriptor_sha256]}" ]]
}
export_compose_identity() {
  export ATELIER2_DEPLOYMENT="${deployment}"
  export ATELIER2_PUBLISHED_PORT="${published_port}"
  export ATELIER2_RESTART_POLICY="${restart_policy}"
  export ATELIER2_SOURCE_COMMIT="${record[source_commit]}"
  export ATELIER2_SOURCE_TREE="${record[source_tree]}"
}

docker_engine_id() {
  docker info --format '{{.ID}}'
}
docker_container_field() {
  docker inspect --type container --format "$1" "${record[container_id]}"
}
resource_label() {
  local resource_type="$1"
  local resource="$2"
  local label="$3"
  docker "${resource_type}" inspect --format "{{index .Labels \"${label}\"}}" "${resource}"
}
resource_identity_is_exact() {
  # Volume and network identity is judged by the commit that created them
  # (record[store_source_commit/tree]), never the commit currently running
  # (record[source_commit/tree]): a preserving `update` keeps both resources
  # in place across every later redeploy, so their creation-time labels stay
  # frozen at the store's origin while the running code moves on.
  local resource_type="$1" resource="$2" expected_commit="$3" expected_tree="$4"
  [[ "$(resource_label "${resource_type}" "${resource}" atelier2.deployment)" == "${deployment}" ]] \
    && [[ "$(resource_label "${resource_type}" "${resource}" atelier2.source.commit)" == "${expected_commit}" ]] \
    && [[ "$(resource_label "${resource_type}" "${resource}" atelier2.source.tree)" == "${expected_tree}" ]]
}
project_resources_are_owned() {
  local resource resource_type resources
  owned_project_has_volume=0
  resources="$(docker ps --all --quiet --filter "label=com.docker.compose.project=${record[project]}")" || return 1
  while IFS= read -r resource; do
    [[ -z "${resource}" ]] && continue
    [[ "$(docker inspect --type container --format '{{index .Config.Labels "atelier2.deployment"}}' "${resource}")" == "${deployment}" ]] || return 1
    [[ "$(docker inspect --type container --format '{{index .Config.Labels "atelier2.source.commit"}}' "${resource}")" == "${record[source_commit]}" ]] || return 1
    [[ "$(docker inspect --type container --format '{{index .Config.Labels "atelier2.source.tree"}}' "${resource}")" == "${record[source_tree]}" ]] || return 1
  done <<<"${resources}"
  for resource_type in volume network; do
    resources="$(docker "${resource_type}" ls --quiet --filter "label=com.docker.compose.project=${record[project]}")" || return 1
    while IFS= read -r resource; do
      [[ -z "${resource}" ]] && continue
      # An INSTALLING record has no store_source_commit/tree yet (they are
      # only finalized once state reaches INSTALLED): a crash-recovery
      # teardown of that half-finished install created its volume/network
      # under the record's own (only) source_commit/tree, so that is the
      # right identity to demand here.
      resource_identity_is_exact "${resource_type}" "${resource}" \
        "${record[store_source_commit]:-${record[source_commit]}}" \
        "${record[store_source_tree]:-${record[source_tree]}}" || return 1
      if [[ "${resource_type}" == "volume" ]]; then
        owned_project_has_volume=1
      fi
    done <<<"${resources}"
  done
}
teardown_recorded_installation() {
  local cleanup_status=0
  [[ "$(docker_engine_id)" == "${record[engine_id]}" ]] || return 1
  descriptor_is_exact || return 1
  project_resources_are_owned || return 1
  export_compose_identity
  docker compose --project-name "${record[project]}" -f "${descriptor_file}" \
    down --volumes --rmi local --remove-orphans || cleanup_status=$?
  unset ATELIER2_DEPLOYMENT ATELIER2_PUBLISHED_PORT ATELIER2_RESTART_POLICY \
    ATELIER2_SOURCE_COMMIT ATELIER2_SOURCE_TREE
  if ((cleanup_status == 0)); then
    rm -f -- "${descriptor_file}" "${record_file}"
    sync -f "${installation_directory}"
  fi
  return "${cleanup_status}"
}
local_live_docker_resource_ids() {
  local resource_type="$1"
  case "${resource_type}" in
    container) docker ps --all --quiet --filter "label=atelier2.deployment=${deployment}" ;;
    network) docker network ls --quiet --filter "label=atelier2.deployment=${deployment}" ;;
    volume) docker volume ls --quiet --filter "label=atelier2.deployment=${deployment}" ;;
    image) docker images --quiet --filter "reference=${project_name_prefix}*" ;;
  esac
}
remove_local_live_docker_resources() {
  local resource_type resource
  for resource_type in container network volume image; do
    while IFS= read -r resource; do
      [[ -z "${resource}" ]] && continue
      case "${resource_type}" in
        container) docker rm --force -- "${resource}" >/dev/null ;;
        network) docker network rm -- "${resource}" >/dev/null ;;
        volume) docker volume rm --force -- "${resource}" >/dev/null ;;
        image) docker rmi --force -- "${resource}" >/dev/null ;;
      esac || return 1
      case "${resource_type}" in
        container) local_live_container_removed=1 ;;
        network) local_live_network_removed=1 ;;
        volume) local_live_volume_removed=1 ;;
        image) local_live_image_removed=1 ;;
      esac
    done < <(local_live_docker_resource_ids "${resource_type}")
  done
}
cleanup_install_process() {
  local original_status="$?"
  trap - EXIT
  trap '' HUP INT TERM
  [[ -z "${temporary_record}" ]] || rm -f -- "${temporary_record}"
  [[ -z "${temporary_descriptor}" ]] || rm -f -- "${temporary_descriptor}"
  [[ -z "${snapshot_root}" ]] || rm -rf -- "${snapshot_root}"
  if ((install_in_progress && !installation_completed)); then
    if ! read_record || [[ "${record[state]}" != "INSTALLING" ]] \
      || ! teardown_recorded_installation; then
      echo "container live: failed install cleanup is incomplete" >&2
    fi
  fi
  exit "${original_status}"
}
cleanup_start_process() {
  local original_status="$?"; trap - EXIT; trap '' HUP INT TERM
  docker stop --time 30 "${record[container_id]}" >/dev/null \
    || echo "container live: failed start cleanup is incomplete" >&2
  exit "${original_status}"
}
cleanup_update_process() {
  local original_status="$?"
  trap - EXIT
  trap '' HUP INT TERM
  [[ -z "${temporary_descriptor}" ]] || rm -f -- "${temporary_descriptor}"
  [[ -z "${snapshot_root}" ]] || rm -rf -- "${snapshot_root}"
  if ((update_old_stopped && !update_completed)); then
    # `compose up` recreates the service container on every update -- the
    # new commit always changes its labels, so its config hash always
    # differs -- which deletes the previous container as part of that same
    # call, before --wait can even fail. A failure at or after `up` runs can
    # therefore find the previous container already gone: check reality
    # before promising a restart nothing can honour.
    if docker inspect --type container --format '{{.Id}}' "${update_old_container_id}" >/dev/null 2>&1; then
      docker start "${update_old_container_id}" >/dev/null \
        || echo "container live: failed update cleanup is incomplete; run: docker start ${update_old_container_id}" >&2
    else
      echo "container live: the store is already migrated and the previous container no longer exists to restart; run 'container_live.sh status' to see the new container's state, then 'uninstall' or 'update' again" >&2
    fi
  fi
  exit "${original_status}"
}
assert_host_units_are_off() {
  local unit output
  for unit in atelier2.service atelier2-live.service; do
    output="$(systemctl --user show "${unit}" --property=LoadState --property=ActiveState --property=UnitFileState --no-pager)" \
      || fail "host service state is unavailable"
    grep -qx 'ActiveState=inactive' <<<"${output}" \
      || fail "host service ${unit} is active"
    if grep -qx 'LoadState=not-found' <<<"${output}"; then
      continue
    fi
    grep -qx 'UnitFileState=disabled' <<<"${output}" \
      || fail "host service ${unit} is enabled"
  done
}

assert_port_is_free() {
  local listeners
  listeners="$(ss -H -ltn "sport = :${published_port}")" \
    || fail "port ${published_port} state is unavailable"
  [[ -z "${listeners}" ]] || fail "port ${published_port} is already in use"
}

assert_no_local_live_resources() {
  local found=""
  found+="$(docker ps --all --quiet --filter "label=atelier2.deployment=${deployment}")"
  found+="$(docker volume ls --quiet --filter "label=atelier2.deployment=${deployment}")"
  found+="$(docker network ls --quiet --filter "label=atelier2.deployment=${deployment}")"
  [[ -z "${found}" ]] || fail "another local-live Docker owner exists"
}

configuration_sha256() {
  docker_container_field '{{json .Config}}' | sha256sum | cut -d ' ' -f 1
}

verify_installed_configuration() {
  [[ "$(docker_engine_id)" == "${record[engine_id]}" ]] || return 1
  descriptor_is_exact || return 1
  [[ "$(docker_container_field '{{.Id}}')" == "${record[container_id]}" ]] || return 1
  [[ "$(docker_container_field '{{.Image}}')" == "${record[image_id]}" ]] || return 1
  [[ "$(docker_container_field '{{index .Config.Labels "atelier2.deployment"}}')" == "${deployment}" ]] || return 1
  [[ "$(docker_container_field '{{index .Config.Labels "atelier2.source.commit"}}')" == "${record[source_commit]}" ]] || return 1
  [[ "$(docker_container_field '{{index .Config.Labels "atelier2.source.tree"}}')" == "${record[source_tree]}" ]] || return 1
  [[ "$(docker_container_field '{{index .Config.Labels "com.docker.compose.project"}}')" == "${record[project]}" ]] || return 1
  [[ "$(docker_container_field '{{.HostConfig.RestartPolicy.Name}}')" == "${restart_policy}" ]] || return 1
  [[ "$(docker_container_field '{{.HostConfig.ReadonlyRootfs}}')" == "true" ]] || return 1
  [[ "$(docker_container_field '{{.HostConfig.Privileged}}')" == "false" ]] || return 1
  [[ "$(docker_container_field '{{json .HostConfig.CapDrop}}')" == '["ALL"]' ]] || return 1
  [[ "$(docker_container_field '{{json .HostConfig.SecurityOpt}}')" == '["no-new-privileges:true"]' ]] || return 1
  [[ "$(docker_container_field '{{len .HostConfig.PortBindings}}')" == "1" ]] || return 1
  [[ "$(docker_container_field '{{with (index (index .HostConfig.PortBindings "8422/tcp") 0)}}{{.HostIp}}:{{.HostPort}}{{end}}')" == "127.0.0.1:${published_port}" ]] || return 1
  [[ "$(docker_container_field '{{range .Mounts}}{{.Type}}|{{.Name}}|{{.Destination}}|{{.RW}}{{end}}')" == "volume|${record[volume_name]}|/var/lib/atelier2/store|true" ]] || return 1
  [[ "$(docker volume inspect --format '{{.Name}}' "${record[volume_name]}")" == "${record[volume_name]}" ]] || return 1
  resource_identity_is_exact volume "${record[volume_name]}" \
    "${record[store_source_commit]}" "${record[store_source_tree]}" || return 1
  [[ "$(docker network inspect --format '{{.Name}}' "${record[network_name]}")" == "${record[network_name]}" ]] || return 1
  [[ "$(docker network inspect --format '{{.Id}}' "${record[network_name]}")" == "${record[network_id]}" ]] || return 1
  resource_identity_is_exact network "${record[network_name]}" \
    "${record[store_source_commit]}" "${record[store_source_tree]}" || return 1
  [[ "$(docker_container_field '{{len .NetworkSettings.Networks}}')" == "1" ]] || return 1
  [[ "$(docker_container_field "{{with index .NetworkSettings.Networks \"${record[network_name]}\"}}{{.NetworkID}}{{end}}")" == "${record[network_id]}" ]] || return 1
  [[ "$(configuration_sha256)" == "${record[configuration_sha256]}" ]]
}

container_runtime_status() {
  docker_container_field '{{.State.Status}}'
}

container_is_healthy() {
  [[ "$(container_runtime_status)" == "running" ]] \
    && [[ "$(docker_container_field '{{.State.Health.Status}}')" == "healthy" ]]
}
load_completed_installation() {
  read_record || fail "installation record drifted"
  [[ "${record[state]}" == "INSTALLED" ]] || fail "installation is incomplete"
  verify_installed_configuration || fail "installation identity drifted"
}
assert_ambient_container_mode_is_forbidden() {
  local variable
  for variable in ATELIER2_DEPLOYMENT ATELIER2_PUBLISHED_PORT ATELIER2_RESTART_POLICY; do
    [[ -z "${!variable+x}" ]] || fail "ambient container mode is forbidden"
  done
}
install_container() {
  assert_ambient_container_mode_is_forbidden
  acquire_install_lock
  if [[ -e "${record_file}" ]]; then
    read_record || fail "installation record drifted"
    if [[ "${record[state]}" == "INSTALLED" ]]; then
      fail "installation already exists"
    fi
    teardown_recorded_installation || fail "failed install cleanup is incomplete"
  elif [[ -e "${descriptor_file}" ]]; then
    fail "installation descriptor exists without its intent"
  fi
  assert_host_units_are_off
  assert_port_is_free
  assert_no_local_live_resources

  snapshot_root="$(mktemp -d "${TMPDIR:-/tmp}/atelier2-live.XXXXXX")"
  trap cleanup_install_process EXIT
  trap 'exit 129' HUP; trap 'exit 130' INT; trap 'exit 143' TERM
  snapshot="${snapshot_root}/source"
  mkdir --mode=0700 -- "${snapshot}"
  if ! read -r source_commit source_tree \
    < <("${repository}/scripts/container_snapshot.sh" "${repository}" "${snapshot}"); then
    fail "source snapshot failed"
  fi
  project="atelier2-live-$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')"
  descriptor_sha256="$(sha256sum -- "${snapshot}/compose.yaml" | cut -d ' ' -f 1)"
  engine_id="$(docker_engine_id)" || fail "Docker engine identity is unavailable"
  record=(
    [record_version]="${record_version}" [state]="INSTALLING"
    [deployment]="${deployment}" [published_port]="${published_port}" [restart_policy]="${restart_policy}"
    [source_commit]="${source_commit}"
    [source_tree]="${source_tree}"
    [project]="${project}"
    [descriptor_sha256]="${descriptor_sha256}"
    [engine_id]="${engine_id}"
  )
  publish_record
  install_in_progress=1
  temporary_descriptor="$(mktemp "${installation_directory}/.compose.XXXXXX")"
  cp -- "${snapshot}/compose.yaml" "${temporary_descriptor}"
  chmod 0600 -- "${temporary_descriptor}"
  sync -f "${temporary_descriptor}"
  mv -f -- "${temporary_descriptor}" "${descriptor_file}"
  temporary_descriptor=""
  sync -f "${installation_directory}"

  export_compose_identity
  compose=(docker compose --project-name "${project}" --project-directory "${snapshot}" -f "${snapshot}/compose.yaml")
  "${compose[@]}" build
  "${compose[@]}" up --detach --wait --wait-timeout 30 --no-build
  container_id="$("${compose[@]}" ps --quiet serve)"
  [[ "${container_id}" =~ ^[0-9a-f]{64}$ ]] || fail "Docker returned an invalid container identity"
  record[container_id]="${container_id}"
  record[image_id]="$(docker_container_field '{{.Image}}')"
  record[volume_name]="${project}_store"
  record[network_name]="${project}_serve"
  record[network_id]="$(docker network inspect --format '{{.Id}}' "${record[network_name]}")"
  record[configuration_sha256]="$(configuration_sha256)"
  record[store_source_commit]="${record[source_commit]}"
  record[store_source_tree]="${record[source_tree]}"
  record[state]="INSTALLED"
  verify_installed_configuration || fail "installed container identity is incomplete"
  container_is_healthy || fail "installed container is not healthy"
  publish_record
  installation_completed=1
  rm -rf -- "${snapshot_root}"
  snapshot_root=""
  trap - EXIT HUP INT TERM
  echo "container live: cockpit -> http://127.0.0.1:${published_port}/atelier/"
}

status_container() {
  if [[ ! -e "${installation_directory}" ]]; then
    echo "INCOMPLETE"
    return
  fi
  if ! validate_private_directory "${installation_directory}" \
    || [[ ! -e "${lock_file}" ]] \
    || ! validate_private_file "${lock_file}" 0; then
    echo "DRIFTED"
    return
  fi
  acquire_existing_lock
  if [[ ! -e "${record_file}" ]]; then
    echo "INCOMPLETE"
    return
  fi
  if ! read_record; then
    echo "DRIFTED"
    return
  fi
  if [[ "${record[state]}" == "INSTALLING" ]]; then
    echo "INCOMPLETE"
    return
  fi
  if ! verify_installed_configuration; then
    echo "DRIFTED"
    return
  fi
  case "$(container_runtime_status)" in
    running)
      if container_is_healthy; then echo "RUNNING"; else echo "DRIFTED"; fi
      ;;
    exited) echo "STOPPED" ;;
    *) echo "DRIFTED" ;;
  esac
}

stop_container() {
  acquire_existing_lock
  load_completed_installation
  case "$(container_runtime_status)" in
    exited) echo "STOPPED" ;;
    running)
      docker stop --time 30 "${record[container_id]}" >/dev/null
      [[ "$(container_runtime_status)" == "exited" ]] \
        || fail "exact container did not stop"
      echo "STOPPED"
      ;;
    *) fail "installation runtime state drifted" ;;
  esac
}

start_container() {
  acquire_existing_lock
  load_completed_installation
  if [[ "$(container_runtime_status)" == "running" ]]; then
    container_is_healthy || fail "running container is not healthy"
    echo "RUNNING"
    return
  fi
  [[ "$(container_runtime_status)" == "exited" ]] \
    || fail "installation runtime state drifted"
  assert_host_units_are_off
  assert_port_is_free
  trap cleanup_start_process EXIT
  trap 'exit 129' HUP; trap 'exit 130' INT; trap 'exit 143' TERM
  docker start "${record[container_id]}" >/dev/null \
    || fail "exact container did not start"
  for _ in $(seq 1 30); do
    if container_is_healthy; then
      trap - EXIT HUP INT TERM
      echo "RUNNING"
      return
    fi
    sleep 1
  done
  fail "exact container did not become healthy"
}

# No signal trap guards this destructive teardown. Every path below only
# removes the durable record (rm -rf "${installation_directory}", or the
# record/descriptor deletion inside teardown_recorded_installation) after the
# Docker call that owns that step has already returned success; `fail` exits
# before that point on any failure. An interruption therefore never finalizes
# "removed" ahead of the destructive call it depends on — it just leaves
# Docker debris for the next `uninstall` to finish. A plain retry is safe and
# idempotent by construction, so no compensating trap is needed here.
uninstall_installation() {
  if [[ ! -e "${installation_directory}" ]]; then
    echo "container live: nothing installed"
    return
  fi
  acquire_install_lock
  local_live_container_removed=0
  local_live_network_removed=0
  local_live_volume_removed=0
  local_live_image_removed=0
  owned_project_has_volume=0
  local cleaned=0 volume_removed=0
  if [[ -e "${record_file}" ]] && read_record && teardown_recorded_installation; then
    cleaned=1
    volume_removed="${owned_project_has_volume}"
  else
    remove_local_live_docker_resources \
      || fail "uninstall could not remove every Docker resource"
    if ((local_live_container_removed || local_live_network_removed \
      || local_live_volume_removed || local_live_image_removed)); then
      cleaned=1
    fi
    volume_removed="${local_live_volume_removed}"
  fi
  rm -rf -- "${installation_directory}"
  uninstalled_existing_store="${volume_removed}"
  if ((cleaned)); then
    uninstalled_existing_installation=1
    echo "container live: uninstalled"
  else
    echo "container live: nothing installed"
  fi
}

built_serve_image_id() {
  local project="$1" reference short_id
  reference="${project}-serve"
  short_id="$(docker images --quiet --filter "reference=${reference}")" \
    || fail "the built image identity is unavailable"
  [[ -n "${short_id}" ]] || fail "the built image identity is unavailable"
  docker inspect --type image --format '{{.Id}}' "${short_id}"
}

# Raises the installed store through the migration ladder (`atelier2
# migrate`, #244) in place, on the same Compose volume and network, then
# starts the new container on the migrated store. The ladder's own contract
# is the backup (see migrate_store's own docstring, and #244's precedent):
# each step is one transaction that either commits completely or leaves the
# file exactly as it was, so there is nothing to separately copy.
#
# The previous container is stopped first to give the ladder exclusive
# access. A failure before `compose up` runs (the stop, or the ladder's own
# refusal) restarts that previous container untouched -- nothing else has
# happened yet. `compose up` itself is not part of that safe window: the new
# commit always changes the service's labels, so its config hash always
# differs from the previous container's, and Compose recreates it --
# deleting the previous container as an intrinsic part of that one call,
# before `--wait` can even time out. A failure at or after `up` therefore
# finds the previous container already gone; cleanup_update_process checks
# for that and reports the true state (store migrated, new container
# unconfirmed) instead of promising a restart nothing can honour. The
# durable record is untouched either way until the very end.
update_preserving_store() {
  acquire_install_lock
  if [[ ! -e "${record_file}" ]]; then
    install_container
    return
  fi
  read_record || fail "installation record drifted"
  if [[ "${record[state]}" != "INSTALLED" ]]; then
    install_container
    return
  fi
  verify_installed_configuration \
    || fail "installed identity drifted; resolve with status, then uninstall or update --fresh"
  assert_host_units_are_off

  update_old_container_id="${record[container_id]}"
  update_old_image_id="${record[image_id]}"
  update_old_stopped=0
  update_completed=0
  snapshot_root="$(mktemp -d "${TMPDIR:-/tmp}/atelier2-live.XXXXXX")"
  trap cleanup_update_process EXIT
  trap 'exit 129' HUP; trap 'exit 130' INT; trap 'exit 143' TERM

  snapshot="${snapshot_root}/source"
  mkdir --mode=0700 -- "${snapshot}"
  if ! read -r source_commit source_tree \
    < <("${repository}/scripts/container_snapshot.sh" "${repository}" "${snapshot}"); then
    fail "source snapshot failed"
  fi
  engine_id="$(docker_engine_id)" || fail "Docker engine identity is unavailable"
  [[ "${engine_id}" == "${record[engine_id]}" ]] || fail "Docker engine identity drifted"
  record[source_commit]="${source_commit}"
  record[source_tree]="${source_tree}"
  record[descriptor_sha256]="$(sha256sum -- "${snapshot}/compose.yaml" | cut -d ' ' -f 1)"

  export_compose_identity
  compose=(docker compose --project-name "${record[project]}" --project-directory "${snapshot}" -f "${snapshot}/compose.yaml")
  "${compose[@]}" build
  new_image_id="$(built_serve_image_id "${record[project]}")"

  docker stop --time 30 "${update_old_container_id}" >/dev/null \
    || fail "the running container did not stop"
  update_old_stopped=1

  if ! migration_report="$(docker run --rm --entrypoint atelier2 \
    --volume "${record[volume_name]}:/var/lib/atelier2/store" "${new_image_id}" \
    migrate --database /var/lib/atelier2/store/atelier.sqlite 2>&1)"; then
    fail "store migration refused; the previous container is restarting: ${migration_report}"
  fi
  echo "container live: ${migration_report//$'\n'/$'\n''container live: '}"

  assert_port_is_free
  "${compose[@]}" up --detach --wait --wait-timeout 30 --no-build
  container_id="$("${compose[@]}" ps --quiet serve)"
  [[ "${container_id}" =~ ^[0-9a-f]{64}$ ]] || fail "Docker returned an invalid container identity"
  record[container_id]="${container_id}"
  record[image_id]="$(docker_container_field '{{.Image}}')"
  record[network_id]="$(docker network inspect --format '{{.Id}}' "${record[network_name]}")"
  record[configuration_sha256]="$(configuration_sha256)"

  # The descriptor is switched to the new snapshot here, deliberately after
  # every Docker mutation and immediately before the final verify+publish: it
  # is the same atomic commit boundary install_container uses, just placed so
  # that every long-running external command (build, migrate, up) still runs
  # while the record and descriptor together describe the *previous* install.
  # Only a crash in the few local writes between here and publish_record
  # could leave the two apart; that already-healthy previous container would
  # then read as DRIFTED rather than lie as healthy -- the safe direction.
  temporary_descriptor="$(mktemp "${installation_directory}/.compose.XXXXXX")"
  cp -- "${snapshot}/compose.yaml" "${temporary_descriptor}"
  chmod 0600 -- "${temporary_descriptor}"
  sync -f "${temporary_descriptor}"
  mv -f -- "${temporary_descriptor}" "${descriptor_file}"
  temporary_descriptor=""
  sync -f "${installation_directory}"

  verify_installed_configuration || fail "updated container identity is incomplete"
  container_is_healthy || fail "updated container is not healthy"
  publish_record
  update_completed=1
  rm -rf -- "${snapshot_root}"
  snapshot_root=""
  trap - EXIT HUP INT TERM

  docker rmi --force -- "${update_old_image_id}" >/dev/null \
    || echo "container live: previous image ${update_old_image_id} was not removed" >&2
  echo "container live: cockpit -> http://127.0.0.1:${published_port}/atelier/"
}

update_installation() {
  assert_ambient_container_mode_is_forbidden
  if ((fresh)); then
    uninstalled_existing_store=0
    uninstall_installation
    if ((uninstalled_existing_store)); then
      echo "container live: --fresh discards the previous store; installing fresh"
    fi
    install_container
  else
    update_preserving_store
  fi
}

case "${command}" in
  install) install_container ;;
  status) status_container ;;
  stop) stop_container ;;
  start) start_container ;;
  uninstall) uninstall_installation ;;
  update) update_installation ;;
esac
