"""What bytes are one host project-root mapping, and how they are identified."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from atelier2.contracts.host_configuration import (
    MAXIMUM_HOST_PROJECT_ROOT_DOCUMENT_BYTES,
    HostConfigurationInvalid,
    HostConfigurationRefusal,
    HostProjectRootAccepted,
    HostProjectRootRevision,
    ProjectId,
    read_host_project_root_document,
)


def project_root_document(
    project_id: str, revision_number: int, root_path: Path
) -> bytes:
    return json.dumps(
        {
            "project_id": project_id,
            "revision_number": revision_number,
            "root_path": str(root_path),
        }
    ).encode("utf-8")


def test_accepted_bytes_are_the_mapping_and_the_hash_of_those_bytes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    document = project_root_document("studio", 1, root)
    verdict = read_host_project_root_document(document)

    assert verdict == HostProjectRootAccepted(ProjectId("studio"), 1, Path(str(root)))
    assert isinstance(verdict, HostProjectRootAccepted)
    first = HostProjectRootRevision(document, verdict)
    second = HostProjectRootRevision(document, verdict)
    assert first.revision_hash == second.revision_hash


def test_a_later_revision_or_another_project_is_a_different_hash(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    first_bytes = project_root_document("studio", 1, root)
    later_bytes = project_root_document("studio", 2, root)
    other_bytes = project_root_document("other", 1, root)
    first_verdict = read_host_project_root_document(first_bytes)
    later_verdict = read_host_project_root_document(later_bytes)
    other_verdict = read_host_project_root_document(other_bytes)
    assert isinstance(first_verdict, HostProjectRootAccepted)
    assert isinstance(later_verdict, HostProjectRootAccepted)
    assert isinstance(other_verdict, HostProjectRootAccepted)
    first = HostProjectRootRevision(first_bytes, first_verdict)
    later = HostProjectRootRevision(later_bytes, later_verdict)
    other = HostProjectRootRevision(other_bytes, other_verdict)

    assert first.revision_hash != later.revision_hash
    assert first.revision_hash != other.revision_hash


REFUSED_DOCUMENTS: tuple[tuple[str, bytes, HostConfigurationRefusal], ...] = (
    (
        "prose",
        b"project studio lives at /tmp/project",
        HostConfigurationRefusal.NOT_A_PROJECT_ROOT_OBJECT,
    ),
    ("an array", b'["studio"]', HostConfigurationRefusal.NOT_A_PROJECT_ROOT_OBJECT),
    (
        "a field nobody reads",
        b'{"project_id":"studio","revision_number":1,"root_path":"/tmp/p","name":"Studio"}',
        HostConfigurationRefusal.UNKNOWN_FIELD,
    ),
    (
        "a relative root",
        b'{"project_id":"studio","revision_number":1,"root_path":"relative"}',
        HostConfigurationRefusal.RELATIVE_ROOT_PATH,
    ),
    (
        "bytes that are not text",
        b"\xff\xfe",
        HostConfigurationRefusal.DOCUMENT_NOT_UTF8,
    ),
)


@pytest.mark.parametrize(
    ("label", "document", "expected"),
    REFUSED_DOCUMENTS,
    ids=[label for label, _, _ in REFUSED_DOCUMENTS],
)
def test_bytes_that_are_no_project_root_mapping_are_refused_by_name(
    label: str, document: bytes, expected: HostConfigurationRefusal
) -> None:
    del label
    verdict = read_host_project_root_document(document)

    assert isinstance(verdict, HostConfigurationInvalid)
    assert verdict.reason is expected


def test_a_document_larger_than_its_bound_is_refused_before_it_is_read() -> None:
    padded = json.dumps(
        {
            "project_id": "x" * MAXIMUM_HOST_PROJECT_ROOT_DOCUMENT_BYTES,
            "revision_number": 1,
            "root_path": "/tmp/p",
        }
    ).encode("utf-8")

    verdict = read_host_project_root_document(padded)

    assert isinstance(verdict, HostConfigurationInvalid)
    assert verdict.reason is HostConfigurationRefusal.DOCUMENT_TOO_LARGE
