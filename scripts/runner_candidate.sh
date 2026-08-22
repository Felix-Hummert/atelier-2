#!/usr/bin/env bash
set -euo pipefail

scenario="${1:-success}"
case "$scenario" in
  success | cancel) ;;
  *)
    printf 'usage: %s [success|cancel]\n' "$0" >&2
    exit 1
    ;;
esac

root=$(mktemp -d /var/tmp/atelier2-301a-runner-witness.XXXXXX)
released=false
identity_volume=""
label="atelier2.runner-candidate=${RANDOM}${RANDOM}"
network="atelier2-301a-${RANDOM}${RANDOM}"
core="${network}-core"
runner="${network}-runner"
printf '%s\n' "$label" >"$root/label"
printf '%s\n' "$network" >"$root/network"
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
  else
    printf 'recovery left labelled objects: label=%s network=%s core=%s runner=%s volume=%s root=%s\n' \
      "$label" "$network" "$core" "$runner" "$identity_volume" "$root" >&2
  fi
}
trap cleanup EXIT
mkdir -p "$root"/{issuer,core-identity,peer,handoff,offer,issuer-output}
printf '%064d\n' 0 >"$root/handoff/manifest-id"
uv run --locked python tests/witness/runner_candidate_issuer.py core --state "$root/issuer" --identity "$root/core-identity"
cp "$root/core-identity/ca.crt" "$root/handoff/ca.crt"
cp "$root/core-identity/core.crt" "$root/handoff/core.crt"
docker build -q -f tests/witness/Dockerfile.runner-core -t atelier2-301a-core . >/dev/null
docker build -q -f tests/witness/Dockerfile.runner -t atelier2-301a-runner . >/dev/null
docker network create --internal --label "$label" "$network" >/dev/null
identity_volume="atelier2-301a-identity-$network"
/usr/bin/docker volume create --driver local --opt type=tmpfs --opt device=tmpfs --opt o=uid=10001,gid=10001,mode=0700,size=65536 --label "$label" "$identity_volume" >/dev/null
volume_options=$(docker volume inspect -f '{{index .Options "o"}}' "$identity_volume")
if [[ "$volume_options" != "uid=10001,gid=10001,mode=0700,size=65536" ]]; then
  printf 'identity volume options differ: %s\n' "$volume_options" >&2
  exit 1
fi
docker run -d --name "$core" --label "$label" --network "$network" --network-alias core.runner-candidate.internal --read-only --tmpfs /tmp:rw,noexec,nosuid,size=16m -v "$root/core-identity:/run/atelier2-core-identity:ro" -v "$root/peer:/run/atelier2-peer-authorization:ro" -v "$root/handoff:/handoff" -v "$root/core-store:/var/lib/atelier2-candidate" atelier2-301a-core --scenario "$scenario" >/dev/null
for _ in $(seq 1 100); do
  [[ -f "$root/handoff/bootstrap.json" ]] && break
  sleep 0.1
done
[[ -f "$root/handoff/bootstrap.json" ]]
runner_id=$(docker run -d --name "$runner" --label "$label" --network "$network" --user 10001:10001 --read-only --cap-drop ALL --security-opt no-new-privileges:true --pids-limit 64 --memory 256m --cpus 1 --tmpfs /journal:rw,noexec,nosuid,size=1m,mode=1777 --tmpfs /offer:rw,noexec,nosuid,size=1m,mode=1777 --mount type=volume,src="$identity_volume",dst=/run/atelier2-identity,readonly,volume-nocopy -v "$root/handoff:/handoff:ro" atelier2-301a-runner)
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
cp "$offer" "$root/handoff/invocation.json"
uv run --locked python tests/witness/runner_candidate_issuer.py runner --state "$root/issuer" --bootstrap "$root/handoff/bootstrap.json" --invocation-offer "$root/offer/invocation.json" --runner-identity "$root/issuer-output" --core-peer "$root/peer"
uv run --locked python tests/witness/runner_candidate_issuer.py receiver-record --identity "$root/issuer-output" | docker run -i --rm --name "${runner}-identity-receiver" --label "$label" --network none --user 10001:10001 --read-only --cap-drop ALL --security-opt no-new-privileges:true --pids-limit 16 --memory 32m --cpus 0.1 --tmpfs /tmp:rw,noexec,nosuid,size=1m --mount type=volume,src="$identity_volume",dst=/identity,volume-nocopy --entrypoint atelier2-runner-identity-receiver atelier2-301a-runner --destination /identity
runner_status=$(docker wait "$runner")
core_status=$(docker wait "$core")
if [[ "$runner_status" == 0 && "$core_status" == 0 ]]; then
  released=true
else
  printf 'candidate did not reach RELEASED: runner=%s core=%s root=%s\n' "$runner_status" "$core_status" "$root" >&2
  exit 1
fi
printf '%s\n' "$root"
