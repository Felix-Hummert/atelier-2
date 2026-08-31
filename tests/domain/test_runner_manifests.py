from __future__ import annotations

from dataclasses import replace
from pathlib import PurePosixPath

import pytest

from atelier2.contracts.hashing import frame
from atelier2.contracts.runner_manifests import (
    CANDIDATE_JOURNAL_BYTES,
    AbsentProviderCli,
    MeasuredProviderCli,
    NoProviderCliPin,
    ProviderCliPin,
    RunnerManifestV1,
    RunnerPathGrant,
    RunnerPathRight,
    decode_measured_provider_cli,
    decode_runner_manifest,
    encode_measured_provider_cli,
    encode_runner_manifest,
    runner_manifest_id,
)
from atelier2.contracts.runner_terminal_evidence_codec import (
    MAXIMUM_RUNNER_TERMINAL_EVIDENCE_RECORD_BYTES,
)


def test_candidate_journal_admits_the_published_v2_terminal_record_bound() -> None:
    assert CANDIDATE_JOURNAL_BYTES == 2_097_152
    assert MAXIMUM_RUNNER_TERMINAL_EVIDENCE_RECORD_BYTES <= CANDIDATE_JOURNAL_BYTES


_CREDENTIALS = PurePosixPath("/run/atelier2-provider-config")
_GRANTS = (
    RunnerPathGrant(PurePosixPath("/proc"), RunnerPathRight.READ_ONLY),
    RunnerPathGrant(_CREDENTIALS, RunnerPathRight.READ_ONLY),
    RunnerPathGrant(PurePosixPath("/tmp"), RunnerPathRight.READ_WRITE),
    RunnerPathGrant(PurePosixPath("/usr"), RunnerPathRight.READ_AND_EXECUTE),
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
        provider_credential_directory=_CREDENTIALS,
        scratch_bytes=67_108_864,
        child_path_grants=_GRANTS,
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
        {"child_path_grants": ()},
        {
            "child_path_grants": (
                RunnerPathGrant(
                    PurePosixPath("/usr"), RunnerPathRight.READ_AND_EXECUTE
                ),
                RunnerPathGrant(_CREDENTIALS, RunnerPathRight.READ_ONLY),
            )
        },
        {
            "child_path_grants": (
                *_GRANTS,
                RunnerPathGrant(PurePosixPath("/usr"), RunnerPathRight.READ_WRITE),
            )
        },
        {"provider_credential_directory": PurePosixPath("/tmp/elsewhere")},
        {
            "child_path_grants": tuple(
                RunnerPathGrant(grant.path, RunnerPathRight.READ_WRITE)
                if grant.path == _CREDENTIALS
                else grant
                for grant in _GRANTS
            )
        },
        {
            "child_path_grants": tuple(
                RunnerPathGrant(grant.path, RunnerPathRight.READ_AND_EXECUTE)
                if grant.path == _CREDENTIALS
                else grant
                for grant in _GRANTS
            )
        },
    ),
    ids=(
        "image-digest",
        "capabilities",
        "process-limit",
        "landlock-abi",
        "source-commit",
        "no-child-surface",
        "unsorted-child-surface",
        "repeated-child-path",
        "credential-directory-outside-the-granted-surface",
        "credential-directory-granted-writable",
        "credential-directory-granted-executable",
    ),
)
def test_manifest_refuses_an_unattestable_fact(change: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        replace(_manifest(), **change)


@pytest.mark.parametrize(
    "path",
    ("relative/path", "/usr/../etc"),
    ids=("relative", "parent-traversal"),
)
def test_path_grant_refuses_a_path_that_is_not_absolute_and_normalized(
    path: str,
) -> None:
    with pytest.raises(ValueError):
        RunnerPathGrant(PurePosixPath(path), RunnerPathRight.READ_ONLY)


def test_manifest_round_trips_as_one_canonical_identity() -> None:
    manifest = _manifest()
    encoded = encode_runner_manifest(manifest)

    assert decode_runner_manifest(encoded) == manifest
    assert runner_manifest_id(manifest).value != "0" * 64


def test_manifest_identity_separates_a_widened_child_surface() -> None:
    manifest = _manifest()
    widened = replace(
        manifest,
        child_path_grants=tuple(
            grant
            if grant.path == _CREDENTIALS
            else RunnerPathGrant(grant.path, RunnerPathRight.READ_WRITE)
            for grant in _GRANTS
        ),
    )

    assert runner_manifest_id(widened) != runner_manifest_id(manifest)
    assert decode_runner_manifest(encode_runner_manifest(widened)) == widened


@pytest.mark.parametrize(
    "measured",
    (AbsentProviderCli(), MeasuredProviderCli((2, 1, 233))),
    ids=("absent", "measured"),
)
def test_measured_provider_cli_round_trips_through_its_wire_field(
    measured: AbsentProviderCli | MeasuredProviderCli,
) -> None:
    assert decode_measured_provider_cli(encode_measured_provider_cli(measured)) == (
        measured
    )


@pytest.mark.parametrize(
    "field",
    (b"", b"2.1", b"2.1.233.1", b"v2.1.233", b"02.1.233", b"none"),
    ids=("empty", "two-parts", "four-parts", "prefixed", "leading-zero", "other-word"),
)
def test_measured_provider_cli_refuses_every_other_field(field: bytes) -> None:
    with pytest.raises(ValueError, match="runner-provider-cli-noncanonical"):
        decode_measured_provider_cli(field)


def test_a_pinned_cli_admits_only_its_conformant_measurements() -> None:
    pin = ProviderCliPin("claude", frozenset({(2, 1, 233)}))

    assert pin.admits(MeasuredProviderCli((2, 1, 233)))
    assert not pin.admits(MeasuredProviderCli((2, 1, 234)))
    assert not pin.admits(AbsentProviderCli())


def test_an_unpinned_executor_admits_only_an_absent_measurement() -> None:
    pin = NoProviderCliPin()

    assert pin.admits(AbsentProviderCli())
    assert not pin.admits(MeasuredProviderCli((2, 1, 233)))


@pytest.mark.parametrize(
    "encoded",
    [
        b"\x00" + encode_runner_manifest(_manifest()),
        frame("runner-manifest/v2", *[b""] * 25),
        encode_runner_manifest(_manifest())[:-2],
    ],
    ids=("foreign-prefix", "another-domain", "truncated-field"),
)
def test_a_broken_frame_is_refused_by_the_manifest_s_own_name(encoded: bytes) -> None:
    """The manifest reads the shared frame layout but keeps its single name for
    every byte sequence that is not one of its manifests."""

    with pytest.raises(ValueError, match="runner-manifest-mismatch"):
        decode_runner_manifest(encoded)
