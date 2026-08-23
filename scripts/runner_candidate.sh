#!/usr/bin/env bash
set -euo pipefail

witness_root_prefix="/var/tmp/atelier2-301a-runner-witness"
candidate_images=(atelier2-301a-core atelier2-301a-runner atelier2-301a-egress)
# Must match `_CRASH_AFTER_PUBLISH_EXIT_CODE` in `src/atelier2/runner/session.py`
# -- the one process-level fact a real `os._exit` and this shell script can
# only share by declared, matching literal.
crash_after_publish_exit_code=92

# The exact hardening every Runner-image container in this witness runs under,
# named once so a probe leg can never accidentally measure something softer
# than the session leg it is supposed to speak about.
runner_hardening=(
  --user 10001:10001
  --read-only
  --cap-drop ALL
  --security-opt no-new-privileges:true
  --pids-limit 64
  --memory 268435456
  --cpu-period 100000
  --cpu-quota 100000
)
# Every size and path below is read out of the manifest contract, so the
# launcher, the attested manifest and the inspect fence share one source.
read -r scratch_directory scratch_bytes credential_directory workspace_bytes < <(
  uv run --locked python -c \
    'from atelier2.contracts.runner_manifests import (
    CANDIDATE_CREDENTIAL_DIRECTORY,
    CANDIDATE_SCRATCH_BYTES,
    CANDIDATE_SCRATCH_DIRECTORY,
    CANDIDATE_WORKSPACE_BYTES,
)
print(
    CANDIDATE_SCRATCH_DIRECTORY,
    CANDIDATE_SCRATCH_BYTES,
    CANDIDATE_CREDENTIAL_DIRECTORY,
    CANDIDATE_WORKSPACE_BYTES,
)'
)
# The one writable surface `CANDIDATE_CHILD_PATH_GRANTS` attests. It is
# `noexec,nosuid` and sized, all three of which the launcher's own inspect
# attestation re-reads: the provider child may write data here and may never
# run it. The credential directory is deliberately NOT here -- ADR 0009 sec. 2
# decided it is a read-only bind, mounted per Attempt below.
runner_writable_surface=(
  --tmpfs "${scratch_directory}:rw,noexec,nosuid,size=${scratch_bytes},mode=1777"
)

build_candidate_images() {
  # The pinned Claude release is read out of `CONFORMANT_CLAUDE_VERSIONS`,
  # which stays the one register of that fact. The runner re-measures the
  # installed executable against that same set before every provider start,
  # so this build argument is never itself the trusted pin.
  local claude_version
  claude_version=$(uv run --locked python -c \
    'from atelier2.adapters.claude_subscription import CONFORMANT_CLAUDE_VERSIONS
print(".".join(str(part) for part in max(CONFORMANT_CLAUDE_VERSIONS)))')
  docker build -q -f tests/witness/Dockerfile.runner-core -t atelier2-301a-core . >/dev/null
  docker build -q --build-arg CLAUDE_VERSION="$claude_version" --target runner \
    -f tests/witness/Dockerfile.runner -t atelier2-301a-runner . >/dev/null
  docker build -q --target network-policy \
    -f tests/witness/Dockerfile.runner -t atelier2-301a-egress . >/dev/null
  printf '%s\n' "$claude_version"
}

# Installs one Attempt's network policy inside a container's own network
# namespace, from a throwaway container that exits immediately. The container
# itself holds no packet-filtering tool and no `CAP_NET_ADMIN`, so it cannot
# alter what this leaves behind.
#
# The Runner may reach outbound HTTPS and DNS, plus its own Attempt subnet for
# Core. Core may reach nothing outbound at all beyond that same subnet: it
# holds the private key and the only store of product truth and has no reason
# to talk to the Internet. Everything else, in either direction, is REJECTed --
# a loud, immediate connection failure the provider CLI's own error handling
# surfaces, never a silent DROP timeout (ADR 0009 sec. 2, 2026-08-23
# amendment). The same REJECT chain is installed for IPv6, so enabling IPv6 on
# a future Attempt network cannot silently open a second, unfiltered path.
install_attempt_network_policy() {
  local target="$1" subnet="$2" role="$3" internet_rules=""
  if [[ "$role" == runner ]]; then
    internet_rules='
iptables -A OUTPUT -p udp --dport 53 -j ACCEPT
iptables -A OUTPUT -p tcp --dport 53 -j ACCEPT
iptables -A OUTPUT -p tcp --dport 443 -j ACCEPT'
  fi
  docker run --rm --network "container:$target" --user 0 \
    --cap-drop ALL --cap-add NET_ADMIN --entrypoint sh atelier2-301a-egress -c "
set -e
iptables -A OUTPUT -o lo -j ACCEPT
iptables -A OUTPUT -d $subnet -j ACCEPT$internet_rules
iptables -A OUTPUT -p tcp -j REJECT --reject-with tcp-reset
iptables -A OUTPUT -j REJECT --reject-with icmp-port-unreachable
iptables -A INPUT -i lo -j ACCEPT
iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A INPUT -s $subnet -j ACCEPT
iptables -A INPUT -p tcp -j REJECT --reject-with tcp-reset
iptables -A INPUT -j REJECT --reject-with icmp-port-unreachable
ip6tables -A OUTPUT -o lo -j ACCEPT
ip6tables -A OUTPUT -j REJECT
ip6tables -A INPUT -i lo -j ACCEPT
ip6tables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
ip6tables -A INPUT -j REJECT
"
}

# Starts one policed container: it is created detached from every network, its
# Attempt policy is installed while it can reach nothing at all, and only then
# is it connected. A container therefore never runs for even one unfiltered
# packet, and a policy that fails to install leaves it unable to reach anything
# rather than running wide open.
start_policed_container() {
  local name="$1" network="$2" subnet="$3" role="$4"
  shift 4
  docker run -d --name "$name" --network none "$@" >/dev/null
  install_attempt_network_policy "$name" "$subnet" "$role"
  # Docker refuses to attach a container that is still in private ("none")
  # mode, so the empty namespace is released only once the policy is in it.
  docker network disconnect none "$name"
  if [[ "$role" == core ]]; then
    docker network connect --alias core.runner-candidate.internal "$network" "$name"
  else
    docker network connect "$network" "$name"
  fi
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
    --network none --entrypoint claude atelier2-301a-runner --version)
  printf 'claude --version in the hardened container: %s\n' "$reported"
  if [[ "$reported" != "$claude_version "* ]]; then
    printf 'the image measured a release outside the pinned conformance set\n' >&2
    exit 1
  fi
  printf -- '--- leg: bubblewrap startability under the session hardening ---\n'
  docker run --rm "${runner_hardening[@]}" "${runner_writable_surface[@]}" \
    --network none --entrypoint bwrap atelier2-301a-runner --version
  local bwrap_status=0
  docker run --rm "${runner_hardening[@]}" "${runner_writable_surface[@]}" \
    --network none --entrypoint bwrap atelier2-301a-runner \
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
    --entrypoint python atelier2-301a-runner /tmp/toolchain_probe.py
}

# The unbilled egress legs: the failure shape ADR 0009 sec. 2's 2026-08-23
# amendment requires this witness to demonstrate for the mechanism it selected.
run_egress_legs() {
  build_candidate_images >/dev/null
  local label first second first_subnet second_subnet first_host second_host address
  label="atelier2.runner-candidate=${RANDOM}${RANDOM}"
  first="atelier2-301a-egress-${RANDOM}${RANDOM}"
  second="atelier2-301a-egress-${RANDOM}${RANDOM}"
  first_host="${first}-probe"
  second_host="${second}-probe"
  docker network create --label "$label" "$first" >/dev/null
  docker network create --label "$label" "$second" >/dev/null
  # An EXIT trap, not a RETURN one: a failing assertion below leaves this
  # function through `exit`, and a RETURN trap would strand these Attempt
  # networks and containers on the host. The names are expanded into the trap
  # now, because by the time it runs this function's own locals are gone.
  trap "docker rm -f '$first_host' '$second_host' >/dev/null 2>&1 || true; docker network rm '$first' '$second' >/dev/null 2>&1 || true" EXIT
  first_subnet=$(docker network inspect -f '{{(index .IPAM.Config 0).Subnet}}' "$first")
  second_subnet=$(docker network inspect -f '{{(index .IPAM.Config 0).Subnet}}' "$second")
  start_policed_container "$first_host" "$first" "$first_subnet" runner \
    --label "$label" "${runner_hardening[@]}" "${runner_writable_surface[@]}" \
    --entrypoint sleep atelier2-301a-runner 300
  start_policed_container "$second_host" "$second" "$second_subnet" runner \
    --label "$label" "${runner_hardening[@]}" "${runner_writable_surface[@]}" \
    --entrypoint sleep atelier2-301a-runner 300
  address=$(docker inspect -f "{{(index .NetworkSettings.Networks \"$first\").IPAddress}}" "$first_host")
  printf 'attempt one: network %s subnet %s address %s\n' "$first" "$first_subnet" "$address"
  printf 'attempt two: network %s subnet %s\n' "$second" "$second_subnet"
  printf -- '--- leg: outbound DNS and HTTPS reach the Internet; everything else is refused ---\n'
  docker run --rm --network "container:$first_host" --entrypoint bash atelier2-301a-runner -c '
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
    --cap-add NET_ADMIN --entrypoint sh atelier2-301a-egress -c '
global=$(ip -6 addr show scope global 2>/dev/null)
route=$(ip -6 route show default 2>/dev/null)
echo "ipv6-global-address=${global:-NONE}"
echo "ipv6-default-route=${route:-NONE}"
# The IPv4 policy above filters IPv4 only. Either this namespace has no global
# IPv6 path at all, or the ip6tables REJECT chain the policy also installs is
# what stands in front of it -- both are asserted, so neither can quietly stop
# being true.
[ -z "$global" ] && [ -z "$route" ] || { echo "UNEXPECTED: a global IPv6 path exists in the attempt namespace"; exit 1; }
ip6tables -C OUTPUT -o lo -j ACCEPT || { echo "UNEXPECTED: no IPv6 policy is installed"; exit 1; }
echo "ipv6-reject-chain=installed"
'
  printf -- '--- leg: inbound into the Attempt container is refused immediately ---\n'
  docker run --rm --network "$first" --entrypoint bash atelier2-301a-runner -c "
set +e
failed=0
for port in 8443 22; do
  started=\$SECONDS; timeout 8 bash -c \"exec 3<>/dev/tcp/$address/\$port\"; rc=\$?
  elapsed=\$((SECONDS - started))
  echo \"inbound-\$port-rc=\$rc seconds=\$elapsed\"
  (( rc != 0 && elapsed < 2 )) || { echo \"UNEXPECTED: inbound port \$port was not refused loudly and immediately\"; failed=1; }
done
exit \$failed
"
  printf -- '--- leg: a second Attempt cannot reach the first, and is refused loudly for trying ---\n'
  docker run --rm --network "container:$second_host" --entrypoint bash atelier2-301a-runner -c "
set +e
failed=0
for port in 8443 22; do
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
      if ! docker image inspect atelier2-301a-core >/dev/null 2>&1; then
        printf 'clean requires the atelier2-301a-core image; run a witness or rebuild before clean\n' >&2
        exit 1
      fi
      # The core container runs as root, so root-owns its bind-mounted
      # core-store; only a root-privileged container can clear it.
      docker run --rm -v "$root:/cleanup" --entrypoint rm atelier2-301a-core -rf -- /cleanup/core-store
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
identity_volume=""
handoff_volume=""
journal_volume=""
label="atelier2.runner-candidate=${RANDOM}${RANDOM}"
network="atelier2-301a-${RANDOM}${RANDOM}"
core="${network}-core"
runner="${network}-runner"
printf '%s\n' "$label" >"$root/label"
printf '%s\n' "$runner" >"$root/runner-container"
printf '%s\n' "$scenario" >"$root/scenario"

cleanup() {
  docker logs "$core" >"$root/core.log" 2>&1 || true
  docker logs "$runner" >"$root/runner.log" 2>&1 || true
  if "$released"; then
    docker rm -f "$runner" "$core" >/dev/null 2>&1 || true
    docker network rm "$network" >/dev/null 2>&1 || true
    if [[ -n "$identity_volume" ]]; then
      docker volume rm "$identity_volume" >/dev/null 2>&1 || true
    fi
    if [[ -n "$handoff_volume" ]]; then
      docker volume rm "$handoff_volume" >/dev/null 2>&1 || true
    fi
    if [[ -n "$journal_volume" ]]; then
      docker volume rm "$journal_volume" >/dev/null 2>&1 || true
    fi
  else
    printf 'recovery left labelled objects: label=%s network=%s core=%s runner=%s volume=%s handoff=%s journal=%s root=%s\n' \
      "$label" "$network" "$core" "$runner" "$identity_volume" "$handoff_volume" "$journal_volume" "$root" >&2
  fi
}
trap cleanup EXIT
mkdir -p "$root"/{issuer,core-identity,peer,handoff,offer,issuer-output,provider-credentials}
chmod 0700 "$root/issuer-output"
uv run --locked python tests/witness/runner_candidate_issuer.py core --state "$root/issuer" --identity "$root/core-identity"
cp "$root/core-identity/ca.crt" "$root/handoff/ca.crt"
cp "$root/core-identity/core.crt" "$root/handoff/core.crt"
build_candidate_images >/dev/null
image_digest=$(docker image inspect -f '{{.Id}}' atelier2-301a-runner)
source_commit=$(git rev-parse HEAD)
uv run --locked python tests/witness/runner_candidate_issuer.py manifest --source-commit "$source_commit" --image-digest "$image_digest" --output "$root/handoff"
# Routed, not `--internal`: this Attempt network reaches the Internet for
# outbound HTTPS and DNS, and `install_attempt_network_policy` refuses
# everything else loudly inside each container's own network namespace (ADR
# 0009 sec. 2, 2026-08-23 amendment). Cross-Attempt unreachability no longer
# rests on `--internal` and is proven live by the `egress` leg instead.
docker network create --label "$label" "$network" >/dev/null
attempt_subnet=$(docker network inspect -f '{{(index .IPAM.Config 0).Subnet}}' "$network")
# Recorded only after the network exists, so a concurrent `clean` never
# mistakes a still-being-created witness for a released one (see the "no
# recorded network" case in `clean`).
printf '%s\n' "$network" >"$root/network"
identity_volume="atelier2-301a-identity-$network"
handoff_volume="atelier2-301a-handoff-$network"
journal_volume="atelier2-301a-journal-$network"
# The identity and journal volumes must survive this exact container's own
# restart across the `resume` scenario's real crash (#15-B5); a tmpfs-backed
# volume does not (verified: it loses its content once no container has it
# mounted, which a stopped container's own restart always crosses). Handoff
# stays tmpfs -- its content is fully reproducible from files already
# retained on the host, and `resume` below just re-copies them.
/usr/bin/docker volume create --driver local --label "$label" "$identity_volume" >/dev/null
/usr/bin/docker volume create --driver local --opt type=tmpfs --opt device=tmpfs --opt o=uid=10001,gid=10001,mode=1777,size=1048576 --label "$label" "$handoff_volume" >/dev/null
/usr/bin/docker volume create --driver local --label "$label" "$journal_volume" >/dev/null
# The "local" driver has no uid/gid/mode option for a non-tmpfs volume, so
# ownership is set once, from a throwaway root container built from the
# already-built candidate image, before either durable volume is ever mounted
# into the UID-10001 Runner.
for durable_volume in "$identity_volume" "$journal_volume"; do
  docker run --rm --user root --mount "type=volume,src=$durable_volume,dst=/target,volume-nocopy" --entrypoint sh atelier2-301a-runner -c 'chown 10001:10001 /target && chmod 0700 /target' >/dev/null
  ownership=$(docker run --rm --user root --mount "type=volume,src=$durable_volume,dst=/target,volume-nocopy" --entrypoint stat atelier2-301a-runner -c '%u:%g:%a' /target)
  if [[ "$ownership" != "10001:10001:700" ]]; then
    printf 'durable volume ownership differs: volume=%s ownership=%s\n' "$durable_volume" "$ownership" >&2
    exit 1
  fi
done
# Core is policed too, and more tightly than the Runner: it holds the private
# key and the only store of product truth, and needs no outbound Internet at
# all. `start_policed_container` creates it detached from every network, so the
# policy is in place before it can send a single packet.
start_policed_container "$core" "$network" "$attempt_subnet" core \
  --label "$label" --read-only --tmpfs "${scratch_directory}:rw,noexec,nosuid,size=16m" \
  -v "$root/core-identity:/run/atelier2-core-identity:ro" \
  -v "$root/peer:/run/atelier2-peer-authorization:ro" \
  -v "$root/handoff:/handoff:ro" \
  -v "$root/core-store:/var/lib/atelier2-candidate" \
  atelier2-301a-core --scenario "$scenario"
core_handoff_rw=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/handoff"}}{{.RW}}{{end}}{{end}}' "$core")
if [[ "$core_handoff_rw" != "false" ]]; then
  printf 'core handoff is not read-only\n' >&2
  exit 1
fi
for _ in $(seq 1 100); do
  if /usr/bin/docker cp "$core:/var/lib/atelier2-candidate/bootstrap.json" "$root/handoff/bootstrap.json" 2>/dev/null \
    && /usr/bin/docker cp "$core:/var/lib/atelier2-candidate/core-peer.json" "$root/handoff/core-peer.json" 2>/dev/null; then
    break
  fi
  rm -f "$root/handoff/bootstrap.json" "$root/handoff/core-peer.json"
  sleep 0.1
done
[[ -s "$root/handoff/bootstrap.json" && -s "$root/handoff/core-peer.json" ]]
# The one extra host surface ADR 0009 sec. 2's 2026-08-22 amendment admits
# beyond the per-invocation identity material: the provider's own credential
# directory, bind-mounted READ-ONLY. This unbilled witness holds no credential,
# so the directory is empty -- but it is mounted in the decided form, and the
# launcher's inspect attestation refuses any other.
mkdir -p "$root/provider-credentials"
chmod 0700 "$root/provider-credentials"
start_policed_container "$runner" "$network" "$attempt_subnet" runner \
  --label "$label" "${runner_hardening[@]}" "${runner_writable_surface[@]}" \
  --tmpfs "/workspace:rw,noexec,nosuid,size=${workspace_bytes},mode=1777" \
  --tmpfs /offer:rw,noexec,nosuid,size=1048576,mode=1777 \
  -v "$root/provider-credentials:${credential_directory}:ro" \
  --mount type=volume,src="$handoff_volume",dst=/handoff,volume-nocopy \
  --mount type=volume,src="$identity_volume",dst=/run/atelier2-identity,readonly,volume-nocopy \
  --mount type=volume,src="$journal_volume",dst=/journal,volume-nocopy \
  atelier2-301a-runner
runner_id=$(docker inspect -f '{{.Id}}' "$runner")

# Handoff is tmpfs-backed and its content is fully reproducible from the host
# files already retained under `$root/handoff`; `resume` below calls this a
# second time to re-populate it after the container's own restart wipes it.
copy_handoff_into_runner() {
  local copied=false
  for _ in $(seq 1 100); do
    if /usr/bin/docker cp "$root/handoff/ca.crt" "$runner:/handoff/ca.crt" \
      && /usr/bin/docker cp "$root/handoff/core.crt" "$runner:/handoff/core.crt" \
      && /usr/bin/docker cp "$root/handoff/manifest" "$runner:/handoff/manifest" \
      && /usr/bin/docker cp "$root/handoff/core-peer.json" "$runner:/handoff/core-peer.json" \
      && /usr/bin/docker cp "$root/handoff/bootstrap.json" "$runner:/handoff/bootstrap.json"; then
      copied=true
      break
    fi
    sleep 0.1
  done
  [[ "$copied" == true ]]
}
copy_handoff_into_runner
offer="$root/offer/invocation.json"
offer_error="$root/offer/error.log"
for _ in $(seq 1 100); do
  : >"$offer_error"
  if /usr/bin/docker exec --user 10001:10001 -- "$runner_id" /usr/bin/cat -- /offer/invocation.json 2>"$offer_error" | dd of="$offer" bs=4097 count=1 status=none; then
    [[ ! -s "$offer_error" ]] && break
  fi
  rm -f "$offer"
  sleep 0.1
done
[[ -s "$offer" && $(wc -c <"$offer") -le 4096 && ! -s "$offer_error" ]]
uv run --locked python tests/witness/runner_candidate_issuer.py runner --state "$root/issuer" --bootstrap "$root/handoff/bootstrap.json" --invocation-offer "$root/offer/invocation.json" --runner-identity "$root/issuer-output" --core-peer "$root/peer"
uv run --locked python tests/witness/runner_candidate_issuer.py receiver-record --identity "$root/issuer-output" | docker run -i --rm --name "${runner}-identity-receiver" --label "$label" --network none --user 10001:10001 --read-only --cap-drop ALL --security-opt no-new-privileges:true --pids-limit 16 --memory 32m --cpus 0.1 --tmpfs /tmp:rw,noexec,nosuid,size=1m --mount type=volume,src="$identity_volume",dst=/identity,volume-nocopy --entrypoint atelier2-runner-identity-receiver atelier2-301a-runner --destination /identity
uv run --locked python tests/witness/runner_candidate_issuer.py unlink-private --key "$root/issuer/ca.key" --key "$root/core-identity/core.key" --key "$root/issuer-output/client.key"
docker inspect "$runner_id" >"$root/runner-inspect.json"
uv run --locked python tests/witness/runner_candidate_issuer.py attest-inspect --inspect "$root/runner-inspect.json" --manifest "$root/handoff/manifest" --output "$root/handoff/inspect-attested"
runner_status=$(docker wait "$runner")
if [[ "$scenario" == "crash-after-publish" ]]; then
  if [[ "$runner_status" != "$crash_after_publish_exit_code" ]]; then
    printf 'runner did not exit at the declared crash cut: runner=%s expected=%s root=%s\n' \
      "$runner_status" "$crash_after_publish_exit_code" "$root" >&2
    exit 1
  fi
  # The literal "terminal-record" must match `_RECORD_NAME` in
  # `src/atelier2/adapters/runner_journal.py` -- the one filename fact this
  # shell probe and that Python module can only share by declared, matching
  # literal, the same way `crash_after_publish_exit_code` above does.
  journal_record=$(docker run --rm --user root --mount "type=volume,src=$journal_volume,dst=/journal,volume-nocopy" --entrypoint sh atelier2-301a-runner -c '[ -f /journal/terminal-record ] && echo present || echo absent')
  if [[ "$journal_record" != "present" ]]; then
    printf 'journal did not retain a terminal record across the crash: root=%s\n' "$root" >&2
    exit 1
  fi
  printf 'observed the declared crash after journal.publish; journal retained its terminal record: runner-exit=%s root=%s\n' \
    "$runner_status" "$root"
  docker start "$runner" >/dev/null
  # A restarted container gets a fresh network namespace, so this Attempt's
  # policy has to be installed into it again before it resumes. Its network
  # attachment survives the restart, so unlike the first start this cannot put
  # the policy in first -- the resumed Runner does nothing on the wire until
  # the handoff below is copied back in.
  install_attempt_network_policy "$runner" "$attempt_subnet" runner
  copy_handoff_into_runner
  printf 'restarted the runner container with its identity and journal volumes intact: runner=%s root=%s\n' \
    "$runner" "$root"
  runner_status=$(docker wait "$runner")
fi
core_status=$(docker wait "$core")
if [[ "$runner_status" == 0 && "$core_status" == 0 ]]; then
  if find "$root" -type f -name '*.key' 2>/dev/null | grep -q .; then
    printf 'witness retained a private key: root=%s\n' "$root" >&2
    exit 1
  fi
  released=true
  if [[ "$scenario" == "crash-after-publish" ]]; then
    printf 'resume delivered the retained evidence through RELEASED: runner=%s core=%s root=%s\n' \
      "$runner_status" "$core_status" "$root"
  fi
else
  printf 'candidate did not reach RELEASED: runner=%s core=%s root=%s\n' "$runner_status" "$core_status" "$root" >&2
  exit 1
fi
printf '%s\n' "$root"
