"""What a published `adapter_operation` revision has to say to be performable.

The seam that consumes the reading is the reference resolution -- the same door
that already refuses a `tool` revision that is not a grant -- so the refusals
are measured there rather than only on the function.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from atelier2.application.resolve_references import (
    ReferenceResolution,
    resolve_declared_reference,
)
from atelier2.contracts.adapter_operations_v3 import (
    MAXIMUM_ADAPTER_OPERATION_DOCUMENT_BYTES,
    AdapterOperationAccepted,
    AdapterOperationName,
    AdapterOperationRefusal,
    AdapterOperationRefused,
    read_adapter_operation_document,
)
from atelier2.contracts.effect_requests import GitCommitIdentity
from atelier2.contracts.revisions_v3 import (
    PublishedRevision,
    PublishedRevisionHash,
    RevisionKind,
)
from atelier2.contracts.run_configuration_v3 import (
    DeclaredReference,
    ReferenceRefusal,
    ReferenceRefusalReason,
    ReferenceSite,
    ResolvedReference,
)
from atelier2.contracts.workflows_v3 import VersionedReference
from atelier2.ports.published_revisions import (
    PublishedRevisionFound,
    PublishedRevisionMissing,
    PublishRevisionResult,
    ResolvePublishedRevisionResult,
)

THE_ONE_OPERATION = json.dumps(
    {"operation": AdapterOperationName.OPEN_PR.value}
).encode("utf-8")

NODE = "publish"


@dataclass(frozen=True)
class OneRevisionRegistry:
    """A registry carrying exactly the revision under test, and nothing else."""

    revision: PublishedRevision

    def publish_revision(self, revision: PublishedRevision) -> PublishRevisionResult:
        raise AssertionError("resolution never publishes")

    def resolve(
        self, kind: RevisionKind, revision_hash: PublishedRevisionHash
    ) -> ResolvePublishedRevisionResult:
        if kind is self.revision.kind and revision_hash == self.revision.revision_hash:
            return PublishedRevisionFound(self.revision)
        return PublishedRevisionMissing()


def resolution_of(document: bytes) -> ReferenceResolution:
    """What the seam a run start uses answers for these exact published bytes."""
    revision = PublishedRevision(RevisionKind.ADAPTER_OPERATION, document)
    declared = DeclaredReference(
        ReferenceSite("operation", NODE, None),
        RevisionKind.ADAPTER_OPERATION,
        VersionedReference(ref="open-pr", revision=revision.revision_hash.value),
    )
    return resolve_declared_reference(declared, OneRevisionRegistry(revision))


def test_open_pr_operation_is_read_and_resolves() -> None:
    assert read_adapter_operation_document(
        THE_ONE_OPERATION
    ) == AdapterOperationAccepted(AdapterOperationName.OPEN_PR)
    assert isinstance(resolution_of(THE_ONE_OPERATION), ResolvedReference)


def test_push_operation_requires_the_declared_git_identities() -> None:
    document = json.dumps(
        {
            "operation": AdapterOperationName.PUSH_ATELIER_COMMIT.value,
            "author": {"name": "Atelier Agent", "email": "agent@example.test"},
            "committer": {
                "name": "Atelier Core",
                "email": "core@example.test",
            },
        }
    ).encode()

    assert read_adapter_operation_document(document) == AdapterOperationAccepted(
        AdapterOperationName.PUSH_ATELIER_COMMIT,
        GitCommitIdentity("Atelier Agent", "agent@example.test"),
        GitCommitIdentity("Atelier Core", "core@example.test"),
    )
    assert isinstance(resolution_of(document), ResolvedReference)


@pytest.mark.parametrize(
    "document",
    (
        b'{"operation":"push-atelier-commit"}',
        b'{"operation":"push-atelier-commit","author":{"name":"Agent","email":"agent@example.test"}}',
        b'{"operation":"push-atelier-commit","author":{"name":"Agent\\nInjected","email":"agent@example.test"},"committer":{"name":"Core","email":"core@example.test"}}',
        b'{"operation":"push-atelier-commit","author":{"name":7,"email":"agent@example.test"},"committer":{"name":"Core","email":"core@example.test"}}',
    ),
)
def test_push_operation_refuses_missing_malformed_or_unsafe_git_identities(
    document: bytes,
) -> None:
    verdict = read_adapter_operation_document(document)

    assert isinstance(verdict, AdapterOperationRefused)
    assert verdict.reason is AdapterOperationRefusal.NOT_AN_OPERATION_OBJECT


NOT_AN_OPERATION_THIS_RUNTIME_PERFORMS: tuple[
    tuple[str, bytes, AdapterOperationRefusal], ...
] = (
    (
        "an operation nothing performs",
        b'{"operation": "merge"}',
        AdapterOperationRefusal.UNKNOWN_OPERATION,
    ),
    (
        "an operation naming no operation",
        b"{}",
        AdapterOperationRefusal.MISSING_OPERATION,
    ),
    (
        "an operation that is not a name",
        b'{"operation": 7}',
        AdapterOperationRefusal.MISSING_OPERATION,
    ),
    (
        "an operation carrying a field nobody reads",
        b'{"operation": "open-pr", "repository": "atelier-2"}',
        AdapterOperationRefusal.UNKNOWN_FIELD,
    ),
    ("prose", b"open a pull request", AdapterOperationRefusal.NOT_AN_OPERATION_OBJECT),
    (
        "an operation that is not an object",
        b'["open-pr"]',
        AdapterOperationRefusal.NOT_AN_OPERATION_OBJECT,
    ),
    ("bytes that are not text", b"\xff\xfe", AdapterOperationRefusal.DOCUMENT_NOT_UTF8),
)


@pytest.mark.proves(
    "an-open-pr-adapter-operation-is-published-and-pinned-by-a-v3-action"
)
@pytest.mark.parametrize(
    ("label", "document", "expected"),
    NOT_AN_OPERATION_THIS_RUNTIME_PERFORMS,
    ids=[label for label, _, _ in NOT_AN_OPERATION_THIS_RUNTIME_PERFORMS],
)
def test_published_bytes_that_are_no_operation_refuse_where_they_were_pinned(
    label: str, document: bytes, expected: AdapterOperationRefusal
) -> None:
    del label
    verdict = read_adapter_operation_document(document)

    assert isinstance(verdict, AdapterOperationRefused)
    assert verdict.reason is expected

    refusal = resolution_of(document)

    assert isinstance(refusal, ReferenceRefusal)
    assert refusal.reason is ReferenceRefusalReason.UNUSABLE_ADAPTER_OPERATION
    assert expected.value in str(refusal)
    assert NODE in str(refusal)


@pytest.mark.proves(
    "an-open-pr-adapter-operation-is-published-and-pinned-by-a-v3-action"
)
def test_an_operation_document_larger_than_its_bound_is_refused_before_it_is_read() -> (
    None
):
    padded = json.dumps(
        {"operation": "x" * MAXIMUM_ADAPTER_OPERATION_DOCUMENT_BYTES}
    ).encode("utf-8")

    verdict = read_adapter_operation_document(padded)

    assert isinstance(verdict, AdapterOperationRefused)
    assert verdict.reason is AdapterOperationRefusal.DOCUMENT_TOO_LARGE
