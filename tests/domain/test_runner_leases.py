"""The Runner lease document contract: the record, its identity, and its codec.

`atelier2.host.runner_launcher` is the one production reader (a lease source
directory today, Serve from `#540` C-3 tomorrow); these tests exercise the
contract on its own, at the layer this shape actually belongs to.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from atelier2.contracts.agent_attempts import (
    AgentAttemptId,
    RunnerGenerationBinding,
    RunnerGenerationId,
)
from atelier2.contracts.agents import AgentExecutionRequestHash
from atelier2.contracts.runner_leases import (
    RunnerLease,
    RunnerLeaseDocument,
    RunnerLeaseDocumentMalformed,
    RunnerLeaseId,
    decode_runner_binding,
    decode_runner_lease_document,
    encode_runner_lease_document,
)
from atelier2.contracts.runner_manifests import (
    RunnerManifestV1,
    candidate_runner_manifest,
    runner_manifest_id,
)

_RUNNER_IMAGE = "atelier2-runner-candidate"
_SERVE_CONTAINER = "atelier2-console"


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


def _binding() -> RunnerGenerationBinding:
    return RunnerGenerationBinding(
        AgentAttemptId("a" * 64),
        AgentExecutionRequestHash("c" * 64),
        RunnerGenerationId("generation-one"),
        runner_manifest_id(_manifest()),
    )


def _document(root: Path) -> RunnerLeaseDocument:
    return RunnerLeaseDocument(
        root / "handoff" / "bootstrap.json",
        root / "handoff" / "manifest",
        _RUNNER_IMAGE,
        _SERVE_CONTAINER,
        root / "handoff",
        root / "peer",
        root / "issuance",
        root / "provider-credentials",
    )


def _document_fields(root: Path) -> dict[str, str]:
    return {
        "binding_path": str(root / "handoff" / "bootstrap.json"),
        "manifest_path": str(root / "handoff" / "manifest"),
        "runner_image": _RUNNER_IMAGE,
        "serve_container": _SERVE_CONTAINER,
        "handoff_directory": str(root / "handoff"),
        "core_peer_directory": str(root / "peer"),
        "issuance_directory": str(root / "issuance"),
        "provider_credential_source": str(root / "provider-credentials"),
    }


def test_a_lease_document_round_trips_through_its_own_encoding(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)

    assert decode_runner_lease_document(encode_runner_lease_document(document)) == (
        document
    )


def test_a_lease_document_missing_a_field_is_refused_by_its_name(
    tmp_path: Path,
) -> None:
    fields = _document_fields(tmp_path)
    del fields["handoff_directory"]

    with pytest.raises(RunnerLeaseDocumentMalformed, match="handoff_directory"):
        decode_runner_lease_document(json.dumps(fields).encode("utf-8"))


def test_a_lease_document_naming_an_unknown_field_is_refused_by_its_name(
    tmp_path: Path,
) -> None:
    fields = _document_fields(tmp_path)
    fields["operator_note"] = "not part of the contract"

    with pytest.raises(RunnerLeaseDocumentMalformed, match="operator_note"):
        decode_runner_lease_document(json.dumps(fields).encode("utf-8"))


def test_a_lease_document_that_is_not_an_object_is_refused(tmp_path: Path) -> None:
    fields = _document_fields(tmp_path)

    with pytest.raises(RunnerLeaseDocumentMalformed, match="not-an-object"):
        decode_runner_lease_document(json.dumps([fields]).encode("utf-8"))


def test_a_lease_document_field_that_is_not_a_string_is_refused_by_its_name(
    tmp_path: Path,
) -> None:
    fields: dict[str, object] = {**_document_fields(tmp_path)}
    fields["runner_image"] = 1

    with pytest.raises(RunnerLeaseDocumentMalformed, match="runner_image"):
        decode_runner_lease_document(json.dumps(fields).encode("utf-8"))


def test_a_witness_shaped_lease_document_decodes_unchanged(tmp_path: Path) -> None:
    """The exact field set `scripts/runner_candidate.sh` writes into
    `leases/open/<lease-id>.json`, with local test values rather than the
    script's own runtime-generated ones (Value Ownership)."""
    attempt_root = tmp_path / "attempt"
    published = json.dumps(
        {
            "binding_path": str(attempt_root / "handoff" / "bootstrap.json"),
            "manifest_path": str(attempt_root / "handoff" / "manifest"),
            "runner_image": _RUNNER_IMAGE,
            "serve_container": "atelier2-301a-lease-one-core",
            "handoff_directory": str(attempt_root / "handoff"),
            "core_peer_directory": str(attempt_root / "peer"),
            "issuance_directory": str(attempt_root / "issuance"),
            "provider_credential_source": str(attempt_root / "provider-credentials"),
        }
    ).encode("utf-8")

    document = decode_runner_lease_document(published)

    assert document.binding_path == attempt_root / "handoff" / "bootstrap.json"
    assert document.manifest_path == attempt_root / "handoff" / "manifest"
    assert document.runner_image == _RUNNER_IMAGE
    assert document.serve_container == "atelier2-301a-lease-one-core"
    assert document.handoff_directory == attempt_root / "handoff"
    assert document.core_peer_directory == attempt_root / "peer"
    assert document.issuance_directory == attempt_root / "issuance"
    assert document.provider_credential_source == attempt_root / "provider-credentials"


def test_a_lease_id_round_trips_through_its_own_value() -> None:
    lease_id = RunnerLeaseId("a" * 64)

    assert lease_id.value == "a" * 64
    assert RunnerLeaseId(lease_id.value) == lease_id


@pytest.mark.parametrize(
    "malformed",
    (
        pytest.param("a" * 63 + ",", id="a-comma"),
        pytest.param("a" * 63 + " ", id="a-space"),
        pytest.param("-" + "a" * 63, id="a-leading-dash"),
        pytest.param("A" * 64, id="uppercase"),
        pytest.param("a" * 63, id="one-character-short"),
        pytest.param("a" * 65, id="one-character-long"),
    ),
)
def test_a_lease_id_outside_the_character_class_is_refused(malformed: str) -> None:
    """Refused by the identity's own construction -- before the value could
    ever become a container, volume, or label name (`#540` C-3.1 D-4)."""
    with pytest.raises(ValueError, match="RunnerLeaseId"):
        RunnerLeaseId(malformed)


def test_the_bootstrap_codec_decodes_the_binding_core_publishes() -> None:
    manifest = _manifest()
    published = json.dumps(
        {
            "attempt_id": "a" * 64,
            "request_hash": "c" * 64,
            "generation_id": "generation-one",
            "manifest_id": runner_manifest_id(manifest).value,
        }
    ).encode("utf-8")

    binding = decode_runner_binding(published)

    assert binding == RunnerGenerationBinding(
        AgentAttemptId("a" * 64),
        AgentExecutionRequestHash("c" * 64),
        RunnerGenerationId("generation-one"),
        runner_manifest_id(manifest),
    )


def test_a_lease_derives_every_object_name_from_its_id(tmp_path: Path) -> None:
    lease = RunnerLease(
        RunnerLeaseId("a" * 64),
        _binding(),
        _RUNNER_IMAGE,
        _manifest(),
        _SERVE_CONTAINER,
        tmp_path / "handoff",
        tmp_path / "peer",
        tmp_path / "issuance",
        tmp_path / "provider-credentials",
    )

    assert lease.attempt_name == f"atelier2-attempt-{'a' * 64}"
    assert lease.label == f"atelier2.runner-lease={'a' * 64}"
