from __future__ import annotations

from dataclasses import replace

import pytest

from atelier2.contracts.runner_manifests import (
    RunnerManifestV1,
    runner_manifest_id,
)


def _manifest() -> RunnerManifestV1:
    return RunnerManifestV1(
        source_commit="a" * 40,
        image_digest="sha256:" + "b" * 64,
        executor_revision="free-v1",
        executor_operational_identity="free-runner",
        provider_id="fake-free",
        auth_mode="api_key",
        requested_capability="headless",
        required_landlock_abi=1,
        effective_uid=10001,
        effective_gid=10001,
        effective_capabilities="0000000000000000",
        no_new_privileges=True,
        read_only_root=True,
        process_limit=64,
        memory_bytes=268_435_456,
        cpu_quota_microseconds=100_000,
        workspace_bytes=67_108_864,
        journal_bytes=1_048_576,
        terminate_grace_milliseconds=1_000,
        reap_deadline_milliseconds=5_000,
        total_attempt_milliseconds=60_000,
    )


def test_manifest_identity_binds_every_attested_runner_fact() -> None:
    manifest = _manifest()

    assert runner_manifest_id(manifest) == runner_manifest_id(manifest)
    assert runner_manifest_id(
        replace(manifest, memory_bytes=268_435_457)
    ) != runner_manifest_id(manifest)


@pytest.mark.parametrize(
    "change",
    (
        {"image_digest": "sha256:not-a-digest"},
        {"effective_capabilities": "1"},
        {"process_limit": 0},
        {"required_landlock_abi": 0},
        {"source_commit": "not-a-commit"},
    ),
)
def test_manifest_refuses_an_unattestable_fact(change: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        replace(_manifest(), **change)
