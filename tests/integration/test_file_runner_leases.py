"""Serve's write side of a Runner lease directory (`#540` C-3.2).

`atelier2.host.runner_launcher.FileRunnerLeaseSource` is the read side of the
exact same directory convention; these tests drive the write side on its own,
racing it against the launcher's own rename-based claim the way the two
processes would race on a real host -- through the filesystem, never through
an import.
"""

from __future__ import annotations

import os
import time
from dataclasses import replace
from pathlib import Path

import pytest

from atelier2.adapters.file_runner_leases import (
    FileRunnerLeasePublisher,
    RunnerAttemptDirectoryOutsideRoot,
    RunnerLeaseUnknown,
)
from atelier2.contracts.agent_attempts import (
    AgentAttemptId,
    RunnerGenerationBinding,
    RunnerGenerationId,
)
from atelier2.contracts.agents import AgentExecutionRequestHash
from atelier2.contracts.runner_leases import (
    RunnerLeaseId,
    decode_runner_binding,
    decode_runner_lease_document,
)
from atelier2.contracts.runner_manifests import (
    RunnerManifestV1,
    candidate_runner_manifest,
    decode_runner_manifest,
    runner_manifest_id,
)
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable
from atelier2.ports.runner_leases import (
    RunnerInvocationTimedOut,
    RunnerLeaseAlreadyClaimed,
    RunnerLeaseExisting,
    RunnerLeasePublished,
    RunnerLeaseRequest,
    RunnerLeaseWithdrawn,
    RunnerPeerMaterial,
)

_RUNNER_IMAGE = "atelier2-runner-candidate"
_SERVE_CONTAINER = "atelier2-console"
_LEASE_ID = RunnerLeaseId("a" * 64)
_CA_CERTIFICATE = b"ca-certificate-bytes"
_CORE_CERTIFICATE = b"core-certificate-bytes"
_CORE_PEER_DOCUMENT = b'{"session_port": 8443}'


def _manifest() -> RunnerManifestV1:
    return candidate_runner_manifest(
        source_commit="a" * 40,
        image_digest="sha256:" + "b" * 64,
        required_landlock_abi=1,
        executor_revision="fake-free/v1",
        executor_operational_identity="free-runner-candidate",
        provider_id="fake-free",
        auth_mode="api_key",
        requested_capability="headless",
    )


def _binding(manifest: RunnerManifestV1) -> RunnerGenerationBinding:
    return RunnerGenerationBinding(
        AgentAttemptId(_LEASE_ID.value),
        AgentExecutionRequestHash("c" * 64),
        RunnerGenerationId("generation-one"),
        runner_manifest_id(manifest),
    )


def _request(lease_id: RunnerLeaseId = _LEASE_ID) -> RunnerLeaseRequest:
    manifest = _manifest()
    return RunnerLeaseRequest(
        lease_id,
        _binding(manifest),
        manifest,
        _RUNNER_IMAGE,
        _SERVE_CONTAINER,
        _CA_CERTIFICATE,
        _CORE_CERTIFICATE,
        _CORE_PEER_DOCUMENT,
    )


def _publisher(tmp_path: Path) -> FileRunnerLeasePublisher:
    return FileRunnerLeasePublisher(tmp_path / "leases", tmp_path / "attempts")


def _claim(tmp_path: Path, lease_id: RunnerLeaseId = _LEASE_ID) -> None:
    """What `FileRunnerLeaseSource.claim_open_lease` does to the same document."""
    name = f"{lease_id.value}.json"
    (tmp_path / "leases" / "open" / name).rename(tmp_path / "leases" / "claimed" / name)


def _release(tmp_path: Path, lease_id: RunnerLeaseId = _LEASE_ID) -> None:
    """What `FileRunnerLeaseSource.release` does to the same document."""
    name = f"{lease_id.value}.json"
    (tmp_path / "leases" / "claimed" / name).rename(
        tmp_path / "leases" / "released" / name
    )


def test_publish_writes_the_lease_document_and_every_document_its_handoff_names(
    tmp_path: Path,
) -> None:
    publisher = _publisher(tmp_path)
    request = _request()

    result = publisher.publish(request)

    assert result == RunnerLeasePublished(_LEASE_ID)
    published = decode_runner_lease_document(
        (tmp_path / "leases" / "open" / f"{_LEASE_ID.value}.json").read_bytes()
    )
    assert published.runner_image == _RUNNER_IMAGE
    assert published.serve_container == _SERVE_CONTAINER
    attempt_root = tmp_path / "attempts" / _LEASE_ID.value
    assert published.handoff_directory == attempt_root / "handoff"
    assert published.core_peer_directory == attempt_root / "peer"
    assert published.issuance_directory == attempt_root / "issuance"
    assert published.provider_credential_source == attempt_root / "provider-credentials"
    assert decode_runner_binding(published.binding_path.read_bytes()) == request.binding
    assert (
        decode_runner_manifest(published.manifest_path.read_bytes()) == request.manifest
    )
    assert (attempt_root / "handoff" / "ca.crt").read_bytes() == _CA_CERTIFICATE
    assert (attempt_root / "handoff" / "core.crt").read_bytes() == _CORE_CERTIFICATE
    assert (
        attempt_root / "handoff" / "core-peer.json"
    ).read_bytes() == _CORE_PEER_DOCUMENT
    assert list((attempt_root / "peer").iterdir()) == []
    assert list((attempt_root / "issuance").iterdir()) == []
    assert list((attempt_root / "provider-credentials").iterdir()) == []


def test_a_crash_between_the_temporary_write_and_the_rename_never_reveals_the_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from atelier2.adapters import file_runner_leases

    publisher = _publisher(tmp_path)

    def _dies(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated crash between the write and the rename")

    monkeypatch.setattr(file_runner_leases.os, "replace", _dies)

    result = publisher.publish(_request())

    assert result == DurableWriteUnavailable()
    assert list((tmp_path / "leases" / "open").glob("*.json")) == []


def test_a_material_fsync_failure_never_reveals_the_lease_either(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lease document `_reveal` fsyncs lives in a separately declared
    tree from the Attempt's own material (`attempt_root`); a launcher that
    bind-mounts the two apart gets no durability guarantee for one from an
    fsync on the other. A crash that only ever loses the material -- never
    the final rename -- must be refused exactly like one that loses the
    rename itself.
    """
    from atelier2.adapters import file_runner_leases

    publisher = _publisher(tmp_path)

    def _dies(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated fsync failure while the material was written")

    monkeypatch.setattr(file_runner_leases.os, "fsync", _dies)

    result = publisher.publish(_request())

    assert result == DurableWriteUnavailable()
    assert list((tmp_path / "leases" / "open").glob("*.json")) == []


def test_a_fsync_failure_on_the_attempt_root_alone_never_reveals_the_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mkdir(paths.root, ...)` creates this Attempt's one directory entry
    inside `attempt_root`; nothing but `attempt_root`'s own fsync makes that
    entry durable. Failing exactly that fsync -- and no other -- must still
    refuse the publish, not just a failure anywhere in the material tree.
    """
    from atelier2.adapters import file_runner_leases

    publisher = _publisher(tmp_path)
    attempt_root = tmp_path / "attempts"
    real_open = file_runner_leases.os.open
    real_fsync = file_runner_leases.os.fsync
    attempt_root_descriptors: set[int] = set()

    def _tracking_open(path: Path, *args: int, **kwargs: int) -> int:
        descriptor = real_open(path, *args, **kwargs)
        if path == attempt_root:
            attempt_root_descriptors.add(descriptor)
        return descriptor

    def _fails_only_for_the_attempt_root(descriptor: int) -> None:
        if descriptor in attempt_root_descriptors:
            raise OSError("simulated fsync failure on the attempt root itself")
        real_fsync(descriptor)

    monkeypatch.setattr(file_runner_leases.os, "open", _tracking_open)
    monkeypatch.setattr(
        file_runner_leases.os, "fsync", _fails_only_for_the_attempt_root
    )

    result = publisher.publish(_request())

    assert result == DurableWriteUnavailable()
    assert list((tmp_path / "leases" / "open").glob("*.json")) == []


def test_a_second_publish_of_the_same_request_is_idempotent(tmp_path: Path) -> None:
    publisher = _publisher(tmp_path)
    request = _request()
    publisher.publish(request)

    result = publisher.publish(request)

    assert result == RunnerLeaseExisting(_LEASE_ID)


def test_a_second_publish_of_the_same_lease_id_with_different_content_is_refused(
    tmp_path: Path,
) -> None:
    publisher = _publisher(tmp_path)
    publisher.publish(_request())

    result = publisher.publish(replace(_request(), runner_image="a-different-image"))

    assert result == DurableStateCorrupt()


def test_withdraw_before_any_claim_wins_and_removes_the_attempt_directory(
    tmp_path: Path,
) -> None:
    publisher = _publisher(tmp_path)
    publisher.publish(_request())

    result = publisher.withdraw(_LEASE_ID)

    assert result == RunnerLeaseWithdrawn(_LEASE_ID)
    assert not (tmp_path / "leases" / "open" / f"{_LEASE_ID.value}.json").exists()
    assert (tmp_path / "leases" / "withdrawn" / f"{_LEASE_ID.value}.json").is_file()
    assert not (tmp_path / "attempts" / _LEASE_ID.value).exists()


def test_serving_an_invocation_for_a_withdrawn_lease_fails_fast(
    tmp_path: Path,
) -> None:
    """A recovered workflow republishing its own withdrawn lease is answered
    `RunnerLeaseExisting`, and the peer material it waits on was deleted with
    the Attempt: `#584` deferred failing fast there to `#585`. This proves it no
    longer burns the whole accept deadline polling paths `withdraw` removed."""
    publisher = _publisher(tmp_path)
    publisher.publish(_request())
    publisher.withdraw(_LEASE_ID)

    started = time.monotonic()
    result = publisher.serve_one_invocation(_LEASE_ID, deadline_seconds=30.0)
    elapsed = time.monotonic() - started

    assert result == RunnerInvocationTimedOut(_LEASE_ID)
    assert elapsed < 5.0


def test_serving_an_invocation_still_waits_while_the_material_stands(
    tmp_path: Path,
) -> None:
    """A published, not-yet-launched lease is still waited on to its deadline:
    the fast path is only for material that was withdrawn, never for a launcher
    that has simply not written the peer material yet."""
    publisher = _publisher(tmp_path)
    publisher.publish(_request())

    result = publisher.serve_one_invocation(_LEASE_ID, deadline_seconds=0.05)

    assert result == RunnerInvocationTimedOut(_LEASE_ID)


def test_withdraw_is_idempotent_once_it_has_already_won(tmp_path: Path) -> None:
    publisher = _publisher(tmp_path)
    publisher.publish(_request())
    publisher.withdraw(_LEASE_ID)

    result = publisher.withdraw(_LEASE_ID)

    assert result == RunnerLeaseWithdrawn(_LEASE_ID)


def test_withdraw_racing_a_concurrent_withdraw_of_the_same_lease_reports_the_win(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two concurrent `withdraw` calls for the same still-open lease can both
    pass the front `withdrawn` check before either renames anything; exactly
    one wins the rename, and the other's `os.rename` then raises
    `FileNotFoundError` for the same reason -- the document is already gone
    from `open`. That loser is this call's own win too, not an unknown lease.
    """
    from atelier2.adapters import file_runner_leases

    publisher = _publisher(tmp_path)
    publisher.publish(_request())
    real_rename = file_runner_leases.os.rename

    def _a_concurrent_withdraw_wins_first(source: Path, destination: Path) -> None:
        # What a second, truly concurrent `withdraw()` would have done to
        # this exact document by the time this call's own rename runs.
        real_rename(source, destination)
        raise FileNotFoundError(source)

    monkeypatch.setattr(
        file_runner_leases.os, "rename", _a_concurrent_withdraw_wins_first
    )

    result = publisher.withdraw(_LEASE_ID)

    assert result == RunnerLeaseWithdrawn(_LEASE_ID)
    assert (tmp_path / "leases" / "withdrawn" / f"{_LEASE_ID.value}.json").is_file()


def test_withdraw_loses_by_name_to_a_launcher_that_already_claimed_the_lease(
    tmp_path: Path,
) -> None:
    publisher = _publisher(tmp_path)
    publisher.publish(_request())
    _claim(tmp_path)

    result = publisher.withdraw(_LEASE_ID)

    assert result == RunnerLeaseAlreadyClaimed(_LEASE_ID)
    assert (tmp_path / "leases" / "claimed" / f"{_LEASE_ID.value}.json").is_file()
    assert (tmp_path / "attempts" / _LEASE_ID.value).exists()


def test_withdraw_is_idempotent_once_it_has_already_lost(tmp_path: Path) -> None:
    publisher = _publisher(tmp_path)
    publisher.publish(_request())
    _claim(tmp_path)
    publisher.withdraw(_LEASE_ID)

    result = publisher.withdraw(_LEASE_ID)

    assert result == RunnerLeaseAlreadyClaimed(_LEASE_ID)


def test_withdraw_after_a_launcher_released_the_attempt_removes_its_directory(
    tmp_path: Path,
) -> None:
    publisher = _publisher(tmp_path)
    publisher.publish(_request())
    _claim(tmp_path)
    _release(tmp_path)

    result = publisher.withdraw(_LEASE_ID)

    assert result == RunnerLeaseAlreadyClaimed(_LEASE_ID)
    assert not (tmp_path / "attempts" / _LEASE_ID.value).exists()
    assert (tmp_path / "leases" / "released" / f"{_LEASE_ID.value}.json").is_file()


def test_withdraw_of_a_lease_never_published_is_refused_by_name(tmp_path: Path) -> None:
    publisher = _publisher(tmp_path)

    with pytest.raises(RunnerLeaseUnknown):
        publisher.withdraw(_LEASE_ID)


def test_publish_refuses_a_target_outside_the_declared_attempt_root(
    tmp_path: Path,
) -> None:
    publisher = _publisher(tmp_path)
    escape = tmp_path / "outside-the-attempt-root"
    escape.mkdir()
    os.symlink(escape, tmp_path / "attempts" / _LEASE_ID.value)

    with pytest.raises(RunnerAttemptDirectoryOutsideRoot):
        publisher.publish(_request())


def test_serve_one_invocation_returns_the_peer_material_a_launcher_wrote(
    tmp_path: Path,
) -> None:
    publisher = _publisher(tmp_path)
    publisher.publish(_request())
    attempt_root = tmp_path / "attempts" / _LEASE_ID.value
    (attempt_root / "peer" / "client.crt").write_bytes(b"the-runner-leaf")
    (attempt_root / "handoff" / "inspect-attested").write_text("attested-record\n")

    outcome = publisher.serve_one_invocation(_LEASE_ID, deadline_seconds=1.0)

    assert outcome == RunnerPeerMaterial(b"the-runner-leaf", "attested-record\n")


def test_serve_one_invocation_times_out_when_no_launcher_ever_answers(
    tmp_path: Path,
) -> None:
    publisher = _publisher(tmp_path)
    publisher.publish(_request())

    outcome = publisher.serve_one_invocation(_LEASE_ID, deadline_seconds=0.05)

    assert outcome == RunnerInvocationTimedOut(_LEASE_ID)
