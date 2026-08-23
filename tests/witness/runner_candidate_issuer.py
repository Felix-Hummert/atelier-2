"""The #301-A witness's CA hook: a thin command line over the host's own owners.

The certificate authority itself is `atelier2.host.runner_identity` and the
inspect attestation is `atelier2.adapters.docker_carrier`; this script only
gives the witness's shell a way to reach them, plus the one manifest a real
Serve would publish for the generation it bound. No key material and no
decision lives here, and nothing here is ever copied into an image.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from atelier2.adapters.docker_carrier import attest_runner_inspect
from atelier2.adapters.free_runner_executor import FreeRunnerExecutorFactory
from atelier2.adapters.runner_child import REQUIRED_LANDLOCK_ABI
from atelier2.contracts.agent_attempts import RunnerInvocationId
from atelier2.contracts.agents import AgentExecutionCapability, AuthMode
from atelier2.contracts.runner_leases import decode_runner_binding
from atelier2.contracts.runner_manifests import (
    candidate_runner_manifest,
    encode_runner_manifest,
    runner_manifest_id,
)
from atelier2.host.runner_identity import (
    RunnerIdentityAuthority,
    receiver_record,
    unlink_private_keys,
)


def write_candidate_manifest(
    output: Path, source_commit: str, image_digest: str
) -> int:
    factory = FreeRunnerExecutorFactory()
    manifest = candidate_runner_manifest(
        source_commit=source_commit,
        image_digest=image_digest,
        required_landlock_abi=REQUIRED_LANDLOCK_ABI,
        executor_revision=factory.key.executor_revision.value,
        executor_operational_identity=factory.operational_identity.value,
        provider_id=factory.key.provider_id.value,
        auth_mode=AuthMode.API_KEY.value,
        requested_capability=AgentExecutionCapability.HEADLESS.value,
    )
    output.mkdir(parents=True, exist_ok=True)
    (output / "manifest").write_bytes(encode_runner_manifest(manifest))
    (output / "manifest-id").write_text(
        runner_manifest_id(manifest).value + "\n", encoding="ascii"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    core = commands.add_parser("core")
    core.add_argument("--state", type=Path, required=True)
    core.add_argument("--identity", type=Path, required=True)
    runner = commands.add_parser("runner")
    runner.add_argument("--state", type=Path, required=True)
    runner.add_argument("--bootstrap", type=Path, required=True)
    runner.add_argument("--invocation-offer", type=Path, required=True)
    runner.add_argument("--runner-identity", type=Path, required=True)
    runner.add_argument("--core-peer", type=Path, required=True)
    record = commands.add_parser("receiver-record")
    record.add_argument("--identity", type=Path, required=True)
    manifest = commands.add_parser("manifest")
    manifest.add_argument("--source-commit", required=True)
    manifest.add_argument("--image-digest", required=True)
    manifest.add_argument("--output", type=Path, required=True)
    inspect = commands.add_parser("attest-inspect")
    inspect.add_argument("--inspect", type=Path, required=True)
    inspect.add_argument("--manifest", type=Path, required=True)
    inspect.add_argument("--output", type=Path, required=True)
    unlink = commands.add_parser("unlink-private")
    unlink.add_argument("--key", type=Path, action="append", required=True)
    parsed = parser.parse_args()
    if parsed.command == "core":
        RunnerIdentityAuthority(parsed.state).issue_core_identity(parsed.identity)
        return 0
    if parsed.command == "manifest":
        return write_candidate_manifest(
            parsed.output, parsed.source_commit, parsed.image_digest
        )
    if parsed.command == "attest-inspect":
        return attest_runner_inspect(parsed.inspect, parsed.manifest, parsed.output)
    if parsed.command == "unlink-private":
        unlink_private_keys(parsed.key)
        return 0
    if parsed.command == "runner":
        offer = json.loads(parsed.invocation_offer.read_text(encoding="utf-8"))
        RunnerIdentityAuthority(parsed.state).issue_runner_identity(
            decode_runner_binding(parsed.bootstrap.read_bytes()),
            RunnerInvocationId(offer["invocation_id"]),
            parsed.runner_identity,
            parsed.core_peer,
        )
        return 0
    sys.stdout.buffer.write(receiver_record(parsed.identity))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
