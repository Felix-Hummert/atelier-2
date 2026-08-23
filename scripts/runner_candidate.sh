#!/usr/bin/env bash
set -euo pipefail

witness_root_prefix="/var/tmp/atelier2-301a-runner-witness"
core_image="atelier2-301a-core"
runner_image="atelier2-301a-runner"
policy_image="atelier2-301a-egress"
candidate_images=("$core_image" "$runner_image" "$policy_image")
# Must match `_CRASH_AFTER_PUBLISH_EXIT_CODE` in `src/atelier2/runner/session.py`
# -- the one process-level fact a real `os._exit` and this shell script can
# only share by declared, matching literal.
crash_after_publish_exit_code=92

# Every Docker operation this witness performs *as a launcher* goes through the
# one typed owner, `atelier2.adapters.docker_carrier` (`#540` C-2a): argument
# vectors instead of shell strings, typed results, named refusals. What stays a
# direct engine call below is deliberately not a launcher operation -- the image
# builds, the `toolchain` and `egress` probe legs, and the witness-directory
# maintenance of `clean`, all of which measure or tidy from the outside.
carrier() {
  uv run --locked python -m atelier2.adapters.docker_carrier \
    --policy-image "$policy_image" "$@"
}

# Every size, path and limit below is read out of the manifest contract, so the
# launcher, the attested manifest and the inspect fence share one source and no
# number is typed twice.
read -r scratch_directory scratch_bytes credential_directory core_port \
  runner_uid runner_gid process_limit memory_bytes cpu_period cpu_quota < <(
  uv run --locked python -c \
    'from atelier2.adapters.runner_tls import CORE_SESSION_PORT
from atelier2.contracts.runner_manifests import (
    CANDIDATE_CPU_PERIOD,
    CANDIDATE_CPU_QUOTA,
    CANDIDATE_CREDENTIAL_DIRECTORY,
    CANDIDATE_EFFECTIVE_GID,
    CANDIDATE_EFFECTIVE_UID,
    CANDIDATE_MEMORY_BYTES,
    CANDIDATE_PROCESS_LIMIT,
    CANDIDATE_SCRATCH_BYTES,
    CANDIDATE_SCRATCH_DIRECTORY,
)
print(
    CANDIDATE_SCRATCH_DIRECTORY,
    CANDIDATE_SCRATCH_BYTES,
    CANDIDATE_CREDENTIAL_DIRECTORY,
    CORE_SESSION_PORT,
    CANDIDATE_EFFECTIVE_UID,
    CANDIDATE_EFFECTIVE_GID,
    CANDIDATE_PROCESS_LIMIT,
    CANDIDATE_MEMORY_BYTES,
    CANDIDATE_CPU_PERIOD,
    CANDIDATE_CPU_QUOTA,
)'
)
runner_user="${runner_uid}:${runner_gid}"
core_scratch_bytes=16777216
handoff_deadline_seconds=10

# The exact hardening every Runner-image container in this witness runs under,
# stated twice in two vocabularies for one reason: the probe legs speak the
# engine's own flags because they measure the engine directly, and the session
# legs speak the carrier's. Both are built from the same manifest numbers above,
# so a probe leg can never measure something softer than the session leg it is
# supposed to speak about.
runner_hardening=(
  --user "$runner_user"
  --read-only
  --cap-drop ALL
  --security-opt no-new-privileges:true
  --pids-limit "$process_limit"
  --memory "$memory_bytes"
  --cpu-period "$cpu_period"
  --cpu-quota "$cpu_quota"
)
carrier_hardening=(
  --user "$runner_user"
  --read-only
  --cap-drop-all
  --no-new-privileges
  --pids-limit "$process_limit"
  --memory-bytes "$memory_bytes"
  --cpu-period "$cpu_period"
  --cpu-quota "$cpu_quota"
)
# The one writable surface `CANDIDATE_CHILD_PATH_GRANTS` attests. It is
# `noexec,nosuid` and sized, all three of which the launcher's own inspect
# attestation re-reads: the provider child may write data here and may never
# run it. The credential directory is deliberately NOT here -- ADR 0009 sec. 2
# decided it is a read-only bind, mounted per Attempt below.
runner_writable_surface=(
  --tmpfs "${scratch_directory}:rw,noexec,nosuid,size=${scratch_bytes},mode=1777"
)
carrier_writable_surface=(--tmpfs "${scratch_directory}:${scratch_bytes}:1777")

build_candidate_images() {
  # The pinned Claude release is read out of `CONFORMANT_CLAUDE_VERSIONS`,
  # which stays the one register of that fact. The runner re-measures the
  # installed executable against that same set before every provider start,
  # so this build argument is never itself the trusted pin.
  local claude_version
  claude_version=$(uv run --locked python -c \
    'from atelier2.adapters.claude_subscription import CONFORMANT_CLAUDE_VERSIONS
print(".".join(str(part) for part in max(CONFORMANT_CLAUDE_VERSIONS)))')
  docker build -q -f tests/witness/Dockerfile.runner-core -t "$core_image" . >/dev/null
  docker build -q --build-arg CLAUDE_VERSION="$claude_version" --target runner \
    -f tests/witness/Dockerfile.runner -t "$runner_image" . >/dev/null
  docker build -q --target network-policy \
    -f tests/witness/Dockerfile.runner -t "$policy_image" . >/dev/null
  printf '%s\n' "$claude_version"
}

# The unbilled toolchain legs: what the deployed Runner image really measures
# about its own provider toolchain, under exactly the session's hardening and
# with no network, no credential and no identity of any kind. The probe program
# is a read-only harness bind mount into a container that drives no session --
# witness plumbing, never a production mount form (ADR 0009 sec. 2).
run_toolchain_legs() {
  local claude_version probe_root
  claude_version=$(build_candidate_images)
  probe_root=$(mktemp -d "$witness_root_prefix.toolchain.XXXXXX")
  # An EXIT trap, not a RETURN one: a failing assertion below leaves this
  # function through `exit`, which no RETURN trap would ever see. The paths are
  # expanded into the trap now, because by the time it runs this function's own
  # locals are gone.
  trap "rm -rf -- '$probe_root'" EXIT
  cat >"$probe_root/toolchain_probe.py" <<'PROBE'
from atelier2.contracts.runner_manifests import candidate_runner_manifest
from atelier2.runner.executors import attest_runner_provider_toolchain


def manifest(provider_id, executor_revision):
    return candidate_runner_manifest(
        source_commit="a" * 40,
        image_digest="sha256:" + "b" * 64,
        required_landlock_abi=1,
        executor_revision=executor_revision,
        executor_operational_identity="toolchain-probe",
        provider_id=provider_id,
        auth_mode="subscription",
        requested_capability="headless",
    )


# Each expectation is the leg's assertion, not a description of it: an image
# that attested something else fails this probe with a nonzero exit.
EXPECTATIONS = (
    ("fake-free", "fake-free/v1", "MEASURED AbsentProviderCli()"),
    # The version measurement runs before the credential check, so reaching
    # this refusal is itself the proof that the installed release measured
    # inside CONFORMANT_CLAUDE_VERSIONS. What refuses is the absent credential
    # record -- correct, and deliberately named apart from a host where
    # administrator policy can act, which is a much louder claim.
    ("anthropic", "claude-subscription/v1", "REFUSED runner-provider-credential-absent"),
    ("anthropic", "claude-not-installed/v9", "REFUSED runner-toolchain-unpinned"),
)

failed = False
for provider_id, executor_revision, expected in EXPECTATIONS:
    try:
        measured = attest_runner_provider_toolchain(manifest(provider_id, executor_revision))
        observed = f"MEASURED {measured}"
    except ValueError as refusal:
        observed = f"REFUSED {refusal}"
    verdict = "ok" if observed == expected else "UNEXPECTED"
    failed = failed or verdict != "ok"
    print(f"{provider_id} {executor_revision} {observed} [{verdict}, expected {expected}]")
raise SystemExit(1 if failed else 0)
PROBE
  printf 'pinned Claude release from CONFORMANT_CLAUDE_VERSIONS: %s\n' "$claude_version"
  printf -- '--- leg: measured provider CLI version under the session hardening ---\n'
  local reported
  reported=$(docker run --rm "${runner_hardening[@]}" "${runner_writable_surface[@]}" \
    --network none --entrypoint claude "$runner_image" --version)
  printf 'claude --version in the hardened container: %s\n' "$reported"
  if [[ "$reported" != "$claude_version "* ]]; then
    printf 'the image measured a release outside the pinned conformance set\n' >&2
    exit 1
  fi
  printf -- '--- leg: bubblewrap startability under the session hardening ---\n'
  docker run --rm "${runner_hardening[@]}" "${runner_writable_surface[@]}" \
    --network none --entrypoint bwrap "$runner_image" --version
  local bwrap_status=0
  docker run --rm "${runner_hardening[@]}" "${runner_writable_surface[@]}" \
    --network none --entrypoint bwrap "$runner_image" \
    --ro-bind / / --dev /dev --proc /proc --unshare-all /usr/bin/true || bwrap_status=$?
  # Deliberately not asserted either way. This is a measurement the operator
  # rules on: on this host the exit is 1, because Docker's default seccomp
  # profile denies user-namespace creation. Softening the container to make
  # bubblewrap succeed would trade the whole hardening for a probe, so the
  # number is recorded and the ruling is left to the owning item.
  printf 'bwrap namespace start under cap-drop=ALL, no-new-privileges and the default seccomp profile: exit=%s\n' \
    "$bwrap_status"
  printf -- '--- leg: runner-side pre-start toolchain attestation ---\n'
  mkdir -p "$probe_root/provider-credentials"
  docker run --rm "${runner_hardening[@]}" "${runner_writable_surface[@]}" \
    --network none -v "$probe_root/toolchain_probe.py:/tmp/toolchain_probe.py:ro" \
    -v "$probe_root/provider-credentials:${credential_directory}:ro" \
    --entrypoint python "$runner_image" /tmp/toolchain_probe.py
}

# The unbilled egress legs: the failure shape ADR 0009 sec. 2's 2026-08-23
# amendment requires this witness to demonstrate for the mechanism it selected.
# The two probed Attempts are started through the carrier, exactly as a session
# leg's containers are, so what these legs measure is the policy the launcher
# owner installs rather than a second ruleset written for the probe.
run_egress_legs() {
  build_candidate_images >/dev/null
  local label first second first_subnet second_subnet first_host second_host address
  label="atelier2.runner-candidate=${RANDOM}${RANDOM}"
  first="atelier2-301a-egress-${RANDOM}${RANDOM}"
  second="atelier2-301a-egress-${RANDOM}${RANDOM}"
  first_host="${first}-probe"
  second_host="${second}-probe"
  # An EXIT trap, not a RETURN one: a failing assertion below leaves this
  # function through `exit`, and a RETURN trap would strand these Attempt
  # networks and containers on the host. The names are expanded into the trap
  # now, because by the time it runs this function's own locals are gone.
  trap "carrier remove --container '$first_host' --container '$second_host' --network '$first' --network '$second' >/dev/null 2>&1 || true" EXIT
  first_subnet=$(carrier create-network --name "$first" --label "$label")
  second_subnet=$(carrier create-network --name "$second" --label "$label")
  # This Attempt really listens, on the port an Attempt's Core would serve and
  # on one it never would. Probing a container that listens for nothing would
  # make "Connection refused" prove the absent service rather than the policy,
  # and would stay green with the INPUT chain wide open.
  carrier start-policed --name "$first_host" --image "$runner_image" --label "$label" \
    --network "$first" --subnet "$first_subnet" --role runner \
    "${carrier_hardening[@]}" "${carrier_writable_surface[@]}" \
    --entrypoint python --argument=-c --argument "
import socket, threading
def serve(port):
    listener = socket.create_server(('0.0.0.0', port), reuse_port=False)
    while True:
        connection, _peer = listener.accept()
        connection.close()
for port in ($core_port, 22):
    threading.Thread(target=serve, args=(port,), daemon=True).start()
threading.Event().wait(300)
" >/dev/null
  carrier start-policed --name "$second_host" --image "$runner_image" --label "$label" \
    --network "$second" --subnet "$second_subnet" --role runner \
    "${carrier_hardening[@]}" "${carrier_writable_surface[@]}" \
    --entrypoint sleep --argument=300 >/dev/null
  address=$(docker inspect -f "{{(index .NetworkSettings.Networks \"$first\").IPAddress}}" "$first_host")
  printf 'attempt one: network %s subnet %s address %s\n' "$first" "$first_subnet" "$address"
  printf 'attempt two: network %s subnet %s\n' "$second" "$second_subnet"
  printf -- '--- leg: outbound DNS and HTTPS reach the Internet; everything else is refused ---\n'
  docker run --rm --network "container:$first_host" --entrypoint bash "$runner_image" -c '
set +e
failed=0
resolved=$(getent hosts api.anthropic.com)
echo "dns-resolved=${resolved:-NONE}"
[[ -n "$resolved" ]] || { echo "UNEXPECTED: DNS did not resolve in the attempt network"; failed=1; }
started=$SECONDS; timeout 8 bash -c "exec 3<>/dev/tcp/api.anthropic.com/443"; rc=$?
echo "https-443-rc=$rc seconds=$((SECONDS - started))"
(( rc == 0 )) || { echo "UNEXPECTED: outbound HTTPS did not connect"; failed=1; }
for port in 80 25; do
  started=$SECONDS; timeout 8 bash -c "exec 3<>/dev/tcp/1.1.1.1/$port"; rc=$?
  elapsed=$((SECONDS - started))
  echo "outbound-$port-rc=$rc seconds=$elapsed"
  # A refusal must be immediate: a DROP would time out at 8 seconds instead.
  (( rc != 0 && elapsed < 2 )) || { echo "UNEXPECTED: port $port was not refused loudly and immediately"; failed=1; }
done
exit $failed
'
  printf -- '--- leg: the attempt namespace carries no global IPv6 path ---\n'
  docker run --rm --network "container:$first_host" --user 0 --cap-drop ALL \
    --cap-add NET_ADMIN --entrypoint sh "$policy_image" -c '
global=$(ip -6 addr show scope global 2>/dev/null)
route=$(ip -6 route show default 2>/dev/null)
echo "ipv6-global-address=${global:-NONE}"
echo "ipv6-default-route=${route:-NONE}"
# The IPv4 policy above filters IPv4 only. Either this namespace has no global
# IPv6 path at all, or the ip6tables REJECT chain the policy also installs is
# what stands in front of it -- both are asserted, so neither can quietly stop
# being true.
[ -z "$global" ] && [ -z "$route" ] || { echo "UNEXPECTED: a global IPv6 path exists in the attempt namespace"; exit 1; }
ip6tables -C OUTPUT -j REJECT || { echo "UNEXPECTED: no IPv6 reject rule is installed"; exit 1; }
echo "ipv6-reject-chain=installed"
'
  printf -- '--- leg: the probed Attempt really serves both ports on its own loopback ---\n'
  docker run --rm --network "container:$first_host" --entrypoint bash "$runner_image" -c "
set +e
failed=0
for port in $core_port 22; do
  timeout 8 bash -c \"exec 3<>/dev/tcp/127.0.0.1/\$port\"; rc=\$?
  echo \"listener-\$port-on-loopback-rc=\$rc\"
  (( rc == 0 )) || { echo \"UNEXPECTED: no listener on port \$port; a refusal from outside would prove nothing\"; failed=1; }
done
exit \$failed
"
  printf -- '--- leg: inbound into the Attempt container is refused immediately ---\n'
  docker run --rm --network "$first" --entrypoint bash "$runner_image" -c "
set +e
failed=0
for port in $core_port 22; do
  started=\$SECONDS; timeout 8 bash -c \"exec 3<>/dev/tcp/$address/\$port\"; rc=\$?
  elapsed=\$((SECONDS - started))
  echo \"inbound-\$port-rc=\$rc seconds=\$elapsed\"
  (( rc != 0 && elapsed < 2 )) || { echo \"UNEXPECTED: inbound port \$port was not refused loudly and immediately\"; failed=1; }
done
exit \$failed
"
  printf -- '--- leg: a second Attempt cannot reach the first, and is refused loudly for trying ---\n'
  docker run --rm --network "container:$second_host" --entrypoint bash "$runner_image" -c "
set +e
failed=0
for port in $core_port 22; do
  started=\$SECONDS; timeout 8 bash -c \"exec 3<>/dev/tcp/$address/\$port\"; rc=\$?
  elapsed=\$((SECONDS - started))
  echo \"cross-attempt-\$port-rc=\$rc seconds=\$elapsed\"
  # The refusal comes from the second Attempt's own OUTPUT chain: the first
  # Attempt's address is outside this Attempt's subnet and is not port 443 or
  # 53, so its own policy rejects it before a packet ever leaves -- loud and
  # immediate, rather than the silent inter-network DROP that would otherwise
  # make an operator wait out a timeout to learn the same thing.
  (( rc != 0 && elapsed < 2 )) || { echo \"UNEXPECTED: cross-attempt port \$port was not refused loudly and immediately\"; failed=1; }
done
exit \$failed
"
}

mode="${1:-success}"
case "$mode" in
  success | cancel)
    scenario="$mode"
    ;;
  resume)
    scenario="crash-after-publish"
    ;;
  toolchain)
    run_toolchain_legs
    exit 0
    ;;
  egress)
    run_egress_legs
    exit 0
    ;;
  clean)
    removed=0
    shopt -s nullglob
    for root in "$witness_root_prefix".*; do
      [[ -f "$root/network" ]] || continue
      network=$(<"$root/network")
      docker network inspect "$network" >/dev/null 2>&1 && continue
      if ! docker image inspect "$core_image" >/dev/null 2>&1; then
        printf 'clean requires the %s image; run a witness or rebuild before clean\n' \
          "$core_image" >&2
        exit 1
      fi
      # The core container runs as root, so root-owns its bind-mounted
      # core-store; only a root-privileged container can clear it.
      docker run --rm -v "$root:/cleanup" --entrypoint rm "$core_image" -rf -- /cleanup/core-store
      rm -rf -- "$root"
      removed=$((removed + 1))
    done
    printf 'removed %d released witness directories\n' "$removed"
    exit 0
    ;;
  images)
    present=()
    for image in "${candidate_images[@]}"; do
      docker image inspect "$image" >/dev/null 2>&1 && present+=("$image")
    done
    if [[ ${#present[@]} -gt 0 ]]; then
      docker image rm -f "${present[@]}"
    fi
    printf 'removed %d candidate images\n' "${#present[@]}"
    exit 0
    ;;
  *)
    printf 'usage: %s [success|cancel|resume|toolchain|egress|clean|images]\n' "$0" >&2
    exit 1
    ;;
esac

root=$(mktemp -d "$witness_root_prefix.XXXXXX")
released=false
network=""
label="atelier2.runner-candidate=${RANDOM}${RANDOM}"
lease_id="301a${RANDOM}${RANDOM}"
core="atelier2-301a-${lease_id}-core"
printf '%s\n' "$label" >"$root/label"
printf '%s\n' "$lease_id" >"$root/lease"
printf '%s\n' "$scenario" >"$root/scenario"

cleanup() {
  carrier logs --container "$core" --output "$root/core.log" >/dev/null 2>&1 || true
  if "$released"; then
    # Everything the launcher created it removed itself; Core is the one
    # container this witness owns, standing in for the console's own.
    carrier remove --container "$core" >/dev/null 2>&1 || true
  else
    printf 'recovery left labelled objects: label=%s lease=%s core=%s network=%s root=%s\n' \
      "$label" "$lease_id" "$core" "$network" "$root" >&2
  fi
}
trap cleanup EXIT
mkdir -p "$root"/{issuer,core-identity,peer,handoff,offer,issuer-output,provider-credentials,core-store,leases/open}
chmod 0700 "$root/issuer-output" "$root/provider-credentials"
uv run --locked python tests/witness/runner_candidate_issuer.py core --state "$root/issuer" --identity "$root/core-identity"
cp "$root/core-identity/ca.crt" "$root/handoff/ca.crt"
cp "$root/core-identity/core.crt" "$root/handoff/core.crt"
build_candidate_images >/dev/null
image_digest=$(docker image inspect -f '{{.Id}}' "$runner_image")
source_commit=$(git rev-parse HEAD)
uv run --locked python tests/witness/runner_candidate_issuer.py manifest --source-commit "$source_commit" --image-digest "$image_digest" --output "$root/handoff"
# Core stands in for the console's own Serve container: it is started attached
# to no network at all and reaches nothing until the launcher creates this
# Attempt's network and attaches it. Its `/handoff` bind is read-only, which
# the carrier reads back out of the container it created.
carrier start-private --name "$core" --image "$core_image" --label "$label" \
  --read-only --tmpfs "${scratch_directory}:${core_scratch_bytes}" \
  --bind "$root/core-identity:/run/atelier2-core-identity:ro" \
  --bind "$root/peer:/run/atelier2-peer-authorization:ro" \
  --bind "$root/handoff:/handoff:ro" \
  --bind "$root/core-store:/var/lib/atelier2-candidate:rw" \
  --argument=--scenario --argument="$scenario" >/dev/null
carrier copy-from --container "$core" \
  --source /var/lib/atelier2-candidate/bootstrap.json \
  --source /var/lib/atelier2-candidate/core-peer.json \
  --destination "$root/handoff" --deadline-seconds "$handoff_deadline_seconds"
# The lease is what a Serve endpoint will answer with in C-3; here the witness
# writes the same facts as one document. The launcher is what turns it into an
# Attempt -- it holds the Docker authority, this script does not hand it any.
cat >"$root/leases/open/${lease_id}.json" <<LEASE
{
  "binding_path": "$root/handoff/bootstrap.json",
  "manifest_path": "$root/handoff/manifest",
  "runner_image": "$runner_image",
  "serve_container": "$core",
  "handoff_directory": "$root/handoff",
  "core_peer_directory": "$root/peer",
  "issuance_directory": "$root/issuer-output",
  "provider_credential_source": "$root/provider-credentials"
}
LEASE
launcher_status=0
# The launcher believes no lease: it admits only paths under the attempt root
# and only the console container declared here, both of which this witness
# passes as its own disposable directory and its own Core.
uv run --locked python -m atelier2.host.runner_launcher \
  --lease-directory "$root/leases" \
  --certificate-authority-state "$root/issuer" \
  --network-policy-image "$policy_image" \
  --attempt-root "$root" \
  --console-container "$core" --once 2>&1 \
  | tee "$root/launcher.log" || launcher_status=$?
network=$(sed -n 's/^attempt-network=//p' "$root/launcher.log")
if [[ -n "$network" ]]; then
  # Recorded only once the launcher reported the network it created, so a
  # concurrent `clean` never mistakes a still-running witness for a released
  # one (see the "no recorded network" case in `clean`).
  printf '%s\n' "$network" >"$root/network"
fi
# The witness's own disposable CA and Core key: the launcher already unlinked
# the client key it minted, and nothing on this host may outlive the run.
uv run --locked python tests/witness/runner_candidate_issuer.py unlink-private --key "$root/issuer/ca.key" --key "$root/core-identity/core.key"
core_status=$(carrier wait --container "$core")
if [[ "$scenario" == "crash-after-publish" ]]; then
  # The declared crash really happened and the launcher really resumed from
  # it: without these two lines a clean first run would pass this scenario
  # while proving nothing about resume.
  if ! grep -qx "runner-exit=$crash_after_publish_exit_code" "$root/launcher.log"; then
    printf 'the launcher did not observe the declared crash cut: expected=%s root=%s\n' \
      "$crash_after_publish_exit_code" "$root" >&2
    exit 1
  fi
  if ! grep -qx "journal-terminal-record=present" "$root/launcher.log"; then
    printf 'journal did not retain a terminal record across the crash: root=%s\n' "$root" >&2
    exit 1
  fi
fi
if [[ "$launcher_status" == 0 && "$core_status" == 0 ]]; then
  if find "$root" -type f -name '*.key' 2>/dev/null | grep -q .; then
    printf 'witness retained a private key: root=%s\n' "$root" >&2
    exit 1
  fi
  released=true
  if [[ "$scenario" == "crash-after-publish" ]]; then
    printf 'resume delivered the retained evidence through RELEASED: core=%s root=%s\n' \
      "$core_status" "$root"
  fi
else
  printf 'candidate did not reach RELEASED: launcher=%s core=%s root=%s\n' "$launcher_status" "$core_status" "$root" >&2
  exit 1
fi
printf '%s\n' "$root"
