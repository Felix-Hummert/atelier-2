"""The Runner lease document: what a lease source hands one launcher.

A lease is a trusted input (`atelier2.host.runner_launcher`): whoever writes
one is asking a launcher to mount host directories and attach a container to
a network, and asking for something is not authority to get it. This module
owns the one shape every writer commits to and every reader admits -- the
`RunnerLease` record a launcher establishes, the on-disk `RunnerLeaseDocument`
a lease source hands over, and `RunnerLeaseId`, the identity contract a
launcher's own object names will be checked against once `#540` C-3.3 V6
wires it in. Encoding and decoding stay one owner here so a document nobody
wrote in this exact shape is refused by the same code that would have minted
it, and a launcher's own decision about which *paths* a lease may actually
name (`RunnerLeaseValidation`) stays entirely its own -- this module reads
bytes into typed fields and nothing more.

Nothing here imports from `atelier2.host` or `atelier2.adapters`: a contract
is layer-neutral by construction, so the same shape serves a single-host
directory today and a Serve-side publisher from `#540` C-3 without either
importing the other.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from atelier2.contracts.agent_attempts import (
    AgentAttemptId,
    RunnerGenerationBinding,
    RunnerGenerationId,
    RunnerManifestId,
)
from atelier2.contracts.agents import AgentExecutionRequestHash
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.runner_manifests import RunnerManifestV1

_LEASE_LABEL = "atelier2.runner-lease"
_DOCUMENT_FIELDS = (
    "binding_path",
    "manifest_path",
    "runner_image",
    "serve_container",
    "handoff_directory",
    "core_peer_directory",
    "issuance_directory",
    "provider_credential_source",
)


class RunnerLeaseId(Sha256Hash):
    """One lease document's identity: the same 64 lowercase hex form as the
    `AgentAttemptId` it names.

    A launcher derives every container, volume, label, and packet-filter
    chain name of an Attempt from a lease's id (`RunnerLease.label`,
    `RunnerLease.attempt_name`), and those names flow straight into a Docker
    CLI's own argument vector and into the Attempt policy's own shell
    program. A value outside this character class is refused by this type's
    own construction, and `RunnerLease.lease_id` holds this type -- so every
    object name is built from a value this class already accepted, rather
    than from whatever a lease source read (`#540` C-3.3 V6, D-4).
    """


@dataclass(frozen=True, slots=True)
class RunnerLease:
    """One Attempt a launcher may establish, as its source hands it over.

    Everything an Attempt needs and nothing it does not: the generation Core
    already bound, the image and manifest that generation names, and the host
    directories this Attempt's material passes through.
    """

    lease_id: RunnerLeaseId
    binding: RunnerGenerationBinding
    runner_image: str
    manifest: RunnerManifestV1
    serve_container: str
    handoff_directory: Path
    core_peer_directory: Path
    issuance_directory: Path
    provider_credential_source: Path

    @property
    def label(self) -> str:
        """What every carrier object of this Attempt carries, so a launcher
        that died can still be told which objects were its own."""
        return lease_label(self.lease_id)

    @property
    def attempt_name(self) -> str:
        """The one name every object of this Attempt is derived from, so an
        operator reading the host sees which lease each of them belongs to."""
        return f"atelier2-attempt-{self.lease_id.value}"


def lease_label(lease_id: RunnerLeaseId) -> str:
    """The label an Attempt's objects carry, derived from the lease alone."""
    return f"{_LEASE_LABEL}={lease_id.value}"


def decode_runner_binding(document: bytes) -> RunnerGenerationBinding:
    """The generation Core bound for one Attempt, as it publishes it."""
    record = json.loads(document)
    return RunnerGenerationBinding(
        AgentAttemptId(record["attempt_id"]),
        AgentExecutionRequestHash(record["request_hash"]),
        RunnerGenerationId(record["generation_id"]),
        RunnerManifestId(record["manifest_id"]),
    )


class RunnerLeaseDocumentMalformed(ValueError):
    """A lease document lacks a field this codec needs, or carries one it
    does not know -- named so one document a writer got wrong costs exactly
    one refused lease, never a launcher that silently accepted a shape
    nobody wrote."""


@dataclass(frozen=True, slots=True)
class RunnerLeaseDocument:
    """The on-disk shape of one lease, before any of its paths are admitted.

    Every path here is exactly what the document's writer chose -- an
    operator script today, Serve itself from `#540` C-3 -- and reading one is
    already acting on it, so this codec turns bytes into typed fields and
    nothing more. Whether a launcher may actually resolve a path against its
    own declared attempt root is `RunnerLeaseValidation`'s decision, never
    this contract's.
    """

    binding_path: Path
    manifest_path: Path
    runner_image: str
    serve_container: str
    handoff_directory: Path
    core_peer_directory: Path
    issuance_directory: Path
    provider_credential_source: Path


def encode_runner_lease_document(document: RunnerLeaseDocument) -> bytes:
    """The one JSON a lease document commits to, and this codec's minting half."""
    return json.dumps(
        {
            "binding_path": str(document.binding_path),
            "manifest_path": str(document.manifest_path),
            "runner_image": document.runner_image,
            "serve_container": document.serve_container,
            "handoff_directory": str(document.handoff_directory),
            "core_peer_directory": str(document.core_peer_directory),
            "issuance_directory": str(document.issuance_directory),
            "provider_credential_source": str(document.provider_credential_source),
        }
    ).encode("utf-8")


def decode_runner_lease_document(payload: bytes) -> RunnerLeaseDocument:
    """Read one lease document back, naming what is missing or foreign.

    A document a writer got wrong is refused by the field it lacks or the
    field it should never have carried, rather than by a `KeyError` an
    unrelated reader raised two calls later -- the same standard every other
    boundary in this codebase reads external input against.
    """
    try:
        record = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as malformed:
        raise RunnerLeaseDocumentMalformed(
            f"lease-document-not-json: {malformed}"
        ) from malformed
    if not isinstance(record, dict):
        raise RunnerLeaseDocumentMalformed("lease-document-not-an-object")
    present = set(record)
    required = set(_DOCUMENT_FIELDS)
    missing = required - present
    if missing:
        raise RunnerLeaseDocumentMalformed(
            "lease-document-missing-field: " + ", ".join(sorted(missing))
        )
    foreign = present - required
    if foreign:
        raise RunnerLeaseDocumentMalformed(
            "lease-document-foreign-field: " + ", ".join(sorted(foreign))
        )
    return RunnerLeaseDocument(
        Path(_require_string(record, "binding_path")),
        Path(_require_string(record, "manifest_path")),
        _require_string(record, "runner_image"),
        _require_string(record, "serve_container"),
        Path(_require_string(record, "handoff_directory")),
        Path(_require_string(record, "core_peer_directory")),
        Path(_require_string(record, "issuance_directory")),
        Path(_require_string(record, "provider_credential_source")),
    )


def _require_string(record: dict[str, Any], name: str) -> str:
    value = record[name]
    if type(value) is not str:
        raise RunnerLeaseDocumentMalformed(f"lease-document-field-not-a-string: {name}")
    return value
