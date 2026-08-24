"""Serve's write side of a Runner lease directory (`#540` C-3.2).

`atelier2.host.runner_launcher.FileRunnerLeaseSource` claims leases this
module publishes and removes what a launcher established once it is done;
this module owns the other half of the same directory convention --
`open`, `claimed`, `released`, plus one state a launcher never touches:
`withdrawn`, for a lease Serve pulled back before any launcher claimed it.
The two meet only through the files each writes, never through an import:
nothing here imports from `atelier2.host`, and nothing there imports this.

Publishing an Attempt's material happens before its lease ever becomes
visible: every file and directory a launcher's lease document could name is
written first, and the lease document itself -- the one thing a launcher's
own `glob` ever sees -- is revealed last, by a rename no reader can catch
half done. A launcher that finds a lease in `open` therefore never finds one
whose material is not already complete.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from atelier2.contracts.agent_attempts import RunnerGenerationBinding
from atelier2.contracts.runner_leases import (
    RunnerLeaseDocument,
    RunnerLeaseId,
    encode_runner_lease_document,
)
from atelier2.contracts.runner_manifests import encode_runner_manifest
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable
from atelier2.ports.runner_leases import (
    PublishRunnerLeaseResult,
    RunnerInvocationOutcome,
    RunnerInvocationTimedOut,
    RunnerLeaseAlreadyClaimed,
    RunnerLeaseExisting,
    RunnerLeasePublished,
    RunnerLeaseRequest,
    RunnerLeaseWithdrawn,
    RunnerPeerMaterial,
    WithdrawRunnerLeaseResult,
)

_STATE_MODE = 0o700
_BOOTSTRAP_NAME = "bootstrap.json"
_MANIFEST_NAME = "manifest"
_CA_NAME = "ca.crt"
_CORE_NAME = "core.crt"
_CORE_PEER_NAME = "core-peer.json"
_PEER_CLIENT_NAME = "client.crt"
# The exact name `atelier2.host.runner_launcher._attest` writes its
# attestation under, once the launcher's engine call reads the container it
# started back against the manifest Core bound. Not importable from there --
# `adapters` sits below `host` -- so this is this module's own half of the
# same filename convention, the physical contract the two processes already
# share on disk.
_ATTESTATION_NAME = "inspect-attested"
_HANDOFF_DIRECTORY_NAME = "handoff"
_PEER_DIRECTORY_NAME = "peer"
_ISSUANCE_DIRECTORY_NAME = "issuance"
_PROVIDER_CREDENTIALS_DIRECTORY_NAME = "provider-credentials"
_INVOCATION_POLL_SECONDS = 0.02


class RunnerAttemptDirectoryOutsideRoot(ValueError):
    """A lease id resolved to a directory outside the declared attempt root.

    A lease id already carries `RunnerLeaseId`'s strict character class, so
    nothing this module reads can name a path outside its own root by value
    alone; this refuses the one way it still could -- a symbolic link already
    standing where an Attempt's own directory is about to be created.
    """


class RunnerLeaseUnknown(LookupError):
    """No lease of this id was ever published, so there is nothing to withdraw."""

    def __init__(self, lease_id: RunnerLeaseId) -> None:
        super().__init__(f"no lease named {lease_id.value} was ever published")
        self.lease_id = lease_id


def _encode_binding_document(binding: RunnerGenerationBinding) -> bytes:
    """The one JSON `handoff/bootstrap.json` carries.

    Two readers admit exactly this shape: a launcher's own
    `atelier2.contracts.runner_leases.decode_runner_binding`, reached through
    the lease document's `binding_path`, and a Runner's own loose read of the
    same four fields (`atelier2.runner.__main__._binding`). Both are read
    sides of one shape; this is that shape's one write side.
    """
    return json.dumps(
        {
            "attempt_id": binding.attempt_id.value,
            "request_hash": binding.request_hash.value,
            "generation_id": binding.generation_id.value,
            "manifest_id": binding.manifest_id.value,
        }
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class _AttemptPaths:
    root: Path
    handoff: Path
    peer: Path
    issuance: Path
    provider_credentials: Path


def _admitted_attempt_paths(
    attempt_root: Path, lease_id: RunnerLeaseId
) -> _AttemptPaths:
    root = (attempt_root / lease_id.value).resolve()
    if not root.is_relative_to(attempt_root.resolve()):
        raise RunnerAttemptDirectoryOutsideRoot(
            f"lease {lease_id.value} names a directory outside the declared "
            f"attempt root {attempt_root}"
        )
    return _AttemptPaths(
        root,
        root / _HANDOFF_DIRECTORY_NAME,
        root / _PEER_DIRECTORY_NAME,
        root / _ISSUANCE_DIRECTORY_NAME,
        root / _PROVIDER_CREDENTIALS_DIRECTORY_NAME,
    )


class FileRunnerLeasePublisher:
    """Runner leases as documents in a directory: the write side a launcher's
    `FileRunnerLeaseSource` claims from.

    `lease_directory` holds nothing but the lease documents' own lifecycle
    states; `attempt_root` holds nothing but the material those documents
    name, one directory per Attempt. Both are declared once, at construction
    -- the same way a launcher's own `--lease-directory` and `--attempt-root`
    are -- never read out of a document either side hands the other.
    """

    def __init__(self, lease_directory: Path, attempt_root: Path) -> None:
        self._attempt_root = attempt_root
        self._open = lease_directory / "open"
        self._claimed = lease_directory / "claimed"
        self._released = lease_directory / "released"
        self._withdrawn = lease_directory / "withdrawn"
        for state in (self._open, self._claimed, self._released, self._withdrawn):
            state.mkdir(mode=_STATE_MODE, parents=True, exist_ok=True)
        attempt_root.mkdir(mode=_STATE_MODE, parents=True, exist_ok=True)

    def publish(self, request: RunnerLeaseRequest) -> PublishRunnerLeaseResult:
        paths = _admitted_attempt_paths(self._attempt_root, request.lease_id)
        document = RunnerLeaseDocument(
            paths.handoff / _BOOTSTRAP_NAME,
            paths.handoff / _MANIFEST_NAME,
            request.runner_image,
            request.serve_container,
            paths.handoff,
            paths.peer,
            paths.issuance,
            paths.provider_credentials,
        )
        encoded = encode_runner_lease_document(document)
        existing = self._existing_document_bytes(request.lease_id)
        if existing is not None:
            if existing == encoded:
                return RunnerLeaseExisting(request.lease_id)
            return DurableStateCorrupt()
        try:
            self._write_material(paths, request)
            self._reveal(request.lease_id, encoded)
        except OSError:
            return DurableWriteUnavailable()
        return RunnerLeasePublished(request.lease_id)

    def withdraw(self, lease_id: RunnerLeaseId) -> WithdrawRunnerLeaseResult:
        name = f"{lease_id.value}.json"
        if (self._withdrawn / name).is_file():
            return RunnerLeaseWithdrawn(lease_id)
        try:
            os.rename(self._open / name, self._withdrawn / name)
        except FileNotFoundError:
            if (self._withdrawn / name).is_file():
                # A concurrent withdraw of this exact lease won the same race
                # between this call's own two steps: it is gone from `open`
                # for the same reason this call's rename just failed. That is
                # this call's own win too, not an unknown lease.
                return RunnerLeaseWithdrawn(lease_id)
            if (self._claimed / name).is_file():
                return RunnerLeaseAlreadyClaimed(lease_id)
            if (self._released / name).is_file():
                self._remove_attempt_material(lease_id)
                return RunnerLeaseAlreadyClaimed(lease_id)
            raise RunnerLeaseUnknown(lease_id) from None
        self._remove_attempt_material(lease_id)
        return RunnerLeaseWithdrawn(lease_id)

    def serve_one_invocation(
        self, lease_id: RunnerLeaseId, deadline_seconds: float
    ) -> RunnerInvocationOutcome:
        paths = _admitted_attempt_paths(self._attempt_root, lease_id)
        client_certificate_path = paths.peer / _PEER_CLIENT_NAME
        attestation_path = paths.handoff / _ATTESTATION_NAME
        deadline = time.monotonic() + deadline_seconds
        while True:
            if not paths.root.exists():
                # This Attempt's material was withdrawn (`#584`): a recovered
                # workflow republished a lease whose own document sits
                # byte-identical in `withdrawn/`, so it was answered
                # `RunnerLeaseExisting` and no launcher will ever claim it -- the
                # peer material this waits on can never appear. Failing fast here
                # is what keeps that Attempt from burning the whole accept
                # deadline polling paths `withdraw` already deleted, before
                # `#585` converges it. The material is written whole before a
                # lease is ever revealed, so a missing root is a withdrawal, not
                # a publish still in flight.
                return RunnerInvocationTimedOut(lease_id)
            if client_certificate_path.is_file() and attestation_path.is_file():
                return RunnerPeerMaterial(
                    client_certificate_path.read_bytes(),
                    attestation_path.read_text(encoding="ascii"),
                )
            if time.monotonic() >= deadline:
                return RunnerInvocationTimedOut(lease_id)
            time.sleep(_INVOCATION_POLL_SECONDS)

    def _existing_document_bytes(self, lease_id: RunnerLeaseId) -> bytes | None:
        name = f"{lease_id.value}.json"
        for state in (self._open, self._claimed, self._released, self._withdrawn):
            path = state / name
            if path.is_file():
                return path.read_bytes()
        return None

    def _write_material(
        self, paths: _AttemptPaths, request: RunnerLeaseRequest
    ) -> None:
        """Every file and directory a published lease could ever name, made
        durable before `_reveal` runs.

        The lease document `_reveal` fsyncs lives in `lease_directory`, a
        separately declared tree from `attempt_root` -- an operator may bind-
        mount the two apart, so durability of one names nothing about the
        other. A crash between this call and `_reveal` must therefore never
        be able to leave a visible lease pointing at missing or half-written
        material: every file this writes is fsynced on its own descriptor,
        and every directory it creates is fsynced after everything that
        belongs inside it exists, so the directory entry for each is durable
        too -- including `attempt_root` itself, whose own directory entry for
        this Attempt's `root` is exactly what `mkdir(paths.root, ...)` above
        just created and nothing else here makes durable.
        """
        for directory in (
            paths.root,
            paths.handoff,
            paths.peer,
            paths.issuance,
            paths.provider_credentials,
        ):
            directory.mkdir(mode=_STATE_MODE, exist_ok=True)
        self._write_file_durable(
            paths.handoff / _BOOTSTRAP_NAME, _encode_binding_document(request.binding)
        )
        self._write_file_durable(
            paths.handoff / _MANIFEST_NAME, encode_runner_manifest(request.manifest)
        )
        self._write_file_durable(paths.handoff / _CA_NAME, request.ca_certificate)
        self._write_file_durable(paths.handoff / _CORE_NAME, request.core_certificate)
        self._write_file_durable(
            paths.handoff / _CORE_PEER_NAME, request.core_peer_document
        )
        self._sync_directory(paths.handoff)
        self._sync_directory(paths.peer)
        self._sync_directory(paths.issuance)
        self._sync_directory(paths.provider_credentials)
        self._sync_directory(paths.root)
        self._sync_directory(self._attempt_root)

    @staticmethod
    def _write_file_durable(path: Path, payload: bytes) -> None:
        """One file, written and fsynced whole -- overwritable, because a
        retried publish of an already-material lease id writes exactly the
        same bytes again rather than refusing a directory it built itself."""
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o600
        )
        try:
            offset = 0
            while offset < len(payload):
                offset += os.write(descriptor, payload[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _reveal(self, lease_id: RunnerLeaseId, encoded: bytes) -> None:
        """Make one lease document visible, whole, or not at all.

        The temporary name carries neither the `.json` suffix nor a bare
        `<lease-id>` stem, so a launcher's own `glob("*.json")` -- and this
        module's own `_existing_document_bytes` -- never see it, whether the
        write that follows completes or a crash leaves it behind.
        """
        name = f"{lease_id.value}.json"
        temporary = self._open / f".{name}.tmp"
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o600
        )
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, self._open / name)
        self._sync_directory(self._open)

    def _remove_attempt_material(self, lease_id: RunnerLeaseId) -> None:
        paths = _admitted_attempt_paths(self._attempt_root, lease_id)
        if paths.root.exists():
            shutil.rmtree(paths.root)

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
