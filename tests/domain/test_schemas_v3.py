from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import NoReturn

import pytest
from referencing.exceptions import Unretrievable

from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.application.bind_run_configuration import bind_run_configuration
from atelier2.application.compose_preview import compose_preview
from atelier2.application.resolve_references import resolve_declared_reference
from atelier2.contracts import schemas_v3
from atelier2.contracts.agents import AgentBindingSetHash
from atelier2.contracts.capabilities_v3 import (
    AttestedCapabilities,
    PublishedSkills,
)
from atelier2.contracts.composed_preview_v3 import ConfigurationBinding
from atelier2.contracts.revisions_v3 import (
    PublishedRevision,
    PublishedRevisionHash,
    RevisionKind,
)
from atelier2.contracts.run_configuration_v3 import (
    DeclaredReference,
    ReferenceRefusal,
    ReferenceRefusalReason,
    ReferenceResolutionRefused,
    ReferenceSite,
    ResolvedReference,
)
from atelier2.contracts.runs import WorkflowRevisionHash
from atelier2.contracts.schemas_v3 import (
    MAXIMUM_SCHEMA_CONTAINER_DEPTH,
    MAXIMUM_SCHEMA_DOCUMENT_BYTES,
    MAXIMUM_SCHEMA_VALUES,
    SchemaAccepted,
    SchemaDocumentRefusal,
    SchemaRefused,
    SchemaRetrievalAttempted,
    read_schema_document,
    refuse_retrieval,
    schema_registry,
)
from atelier2.contracts.workflow_bindings_v3 import SubworkflowBinding
from atelier2.contracts.workflows_v3 import VersionedReference, WorkflowGraphV3
from atelier2.ports.published_revisions import (
    PublishedRevisionFound,
    PublishedRevisionMissing,
    PublishRevisionResult,
    ResolvePublishedRevisionResult,
)

A_REAL_SCHEMA = b'{"type": "object", "required": ["verdict"]}'
A_LOCAL_REFERENCE = (
    b'{"$defs": {"verdict": {"type": "string"}}, "$ref": "#/$defs/verdict"}'
)


def deeper_than_allowed() -> bytes:
    """One array nested past the container ceiling, and nothing else."""
    levels = MAXIMUM_SCHEMA_CONTAINER_DEPTH + 1
    return ("[" * levels + "]" * levels).encode("utf-8")


def wider_than_allowed() -> bytes:
    """A flat array whose entries alone exceed the value ceiling."""
    return json.dumps([0] * (MAXIMUM_SCHEMA_VALUES + 1)).encode("utf-8")


def larger_than_allowed() -> bytes:
    padding = "x" * MAXIMUM_SCHEMA_DOCUMENT_BYTES
    return json.dumps({"type": "string", "description": padding}).encode("utf-8")


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


def resolution_of(document: bytes, kind: RevisionKind = RevisionKind.SCHEMA):
    """What the one seam with consumers answers for these exact published bytes."""
    revision = PublishedRevision(kind, document)
    declared = DeclaredReference(
        ReferenceSite("outputs.schema", "decide", "verdict"),
        kind,
        VersionedReference(ref="verdict", revision=revision.revision_hash.value),
    )
    return resolve_declared_reference(declared, OneRevisionRegistry(revision), {})


THE_FIVE_THAT_BOUND_CLEANLY: tuple[tuple[str, bytes, SchemaDocumentRefusal], ...] = (
    ("prose", b"Guten Morgen", SchemaDocumentRefusal.DOCUMENT_NOT_JSON),
    (
        "a nonlocal reference",
        b'{"$ref": "https://example.com/verdict.json"}',
        SchemaDocumentRefusal.NONLOCAL_REFERENCE,
    ),
    (
        "a dynamic reference",
        b'{"$id": "urn:x", "$anchor": "a", "$dynamicRef": "#a"}',
        SchemaDocumentRefusal.FORBIDDEN_KEYWORD,
    ),
    ("bytes that are not JSON", b"\xff\xfe", SchemaDocumentRefusal.DOCUMENT_NOT_UTF8),
    (
        "a non-canonical number",
        b'{"const": NaN}',
        SchemaDocumentRefusal.NON_CANONICAL_NUMBER,
    ),
)


@pytest.mark.proves("a-schema-revision-outside-the-profile-is-refused-by-name")
@pytest.mark.parametrize(
    ("label", "document", "expected"),
    THE_FIVE_THAT_BOUND_CLEANLY,
    ids=[label for label, _, _ in THE_FIVE_THAT_BOUND_CLEANLY],
)
def test_a_schema_document_outside_the_profile_is_refused_by_name(
    label: str, document: bytes, expected: SchemaDocumentRefusal
) -> None:
    """Each of the five documents that bound cleanly before now names its own fault."""
    verdict = read_schema_document(document)

    assert isinstance(verdict, SchemaRefused)
    assert verdict.refusal is expected

    refusal = resolution_of(document)

    assert isinstance(refusal, ReferenceRefusal)
    assert refusal.reason is ReferenceRefusalReason.UNUSABLE_SCHEMA_DOCUMENT
    assert expected.value in str(refusal)


@pytest.mark.proves("a-schema-revision-outside-the-profile-is-refused-by-name")
@pytest.mark.parametrize(
    ("document", "expected"),
    (
        (larger_than_allowed(), SchemaDocumentRefusal.DOCUMENT_TOO_LARGE),
        (deeper_than_allowed(), SchemaDocumentRefusal.DOCUMENT_TOO_DEEP),
        (wider_than_allowed(), SchemaDocumentRefusal.TOO_MANY_VALUES),
        (
            "﻿{}".encode(),
            SchemaDocumentRefusal.DOCUMENT_CARRIES_BYTE_ORDER_MARK,
        ),
        (
            b'{"type": "object", "type": "string"}',
            SchemaDocumentRefusal.DUPLICATE_OBJECT_KEY,
        ),
        (b"{", SchemaDocumentRefusal.DOCUMENT_NOT_JSON),
        (b'{"type": 17}', SchemaDocumentRefusal.NOT_A_SCHEMA),
    ),
    ids=(
        "too large",
        "too deep",
        "too many values",
        "byte order mark",
        "duplicate object key",
        "truncated json",
        "not a draft 2020-12 schema",
    ),
)
def test_every_bound_of_the_profile_refuses_by_its_own_name(
    document: bytes, expected: SchemaDocumentRefusal
) -> None:
    verdict = read_schema_document(document)

    assert isinstance(verdict, SchemaRefused)
    assert verdict.refusal is expected


@pytest.mark.parametrize(
    "document",
    (A_REAL_SCHEMA, A_LOCAL_REFERENCE, b"true", b'{"format": "email"}'),
    ids=("an object schema", "a local reference", "a boolean schema", "an annotation"),
)
def test_a_schema_inside_the_profile_is_accepted_and_still_binds(
    document: bytes,
) -> None:
    """The profile refuses what it names and nothing else."""
    assert isinstance(read_schema_document(document), SchemaAccepted)

    resolved = resolution_of(document)

    assert isinstance(resolved, ResolvedReference)
    assert (
        resolved.revision_hash
        == PublishedRevision(RevisionKind.SCHEMA, document).revision_hash
    )


def test_a_revision_of_another_kind_is_never_read_as_a_schema() -> None:
    """Only a `schema` reference reads its bytes; every other kind binds on identity."""
    resolved = resolution_of(b"the standing method of a builder", RevisionKind.PROFILE)

    assert isinstance(resolved, ResolvedReference)


SETTLE = PublishedRevision(RevisionKind.DETERMINISTIC_OPERATION, b"settle the verdict")


def one_node_document(schema: PublishedRevision) -> WorkflowGraphV3:
    """The smallest document that declares one output and pins its schema."""
    document = f"""format_version: 3
name: Settle one verdict
nodes:
  - id: decide
    type: deterministic
    operation: {{ref: settle, revision: {SETTLE.revision_hash.value}}}
    outputs:
      - name: verdict
        schema: {{ref: verdict_schema, revision: {schema.revision_hash.value}}}
"""
    graph = parse_workflow_document(document.encode("utf-8"))
    assert isinstance(graph, WorkflowGraphV3)
    return graph


@dataclass(frozen=True)
class ManyRevisionRegistry:
    revisions: tuple[PublishedRevision, ...]

    def publish_revision(self, revision: PublishedRevision) -> PublishRevisionResult:
        raise AssertionError("resolution never publishes")

    def resolve(
        self, kind: RevisionKind, revision_hash: PublishedRevisionHash
    ) -> ResolvePublishedRevisionResult:
        for revision in self.revisions:
            if revision.kind is kind and revision.revision_hash == revision_hash:
                return PublishedRevisionFound(revision)
        return PublishedRevisionMissing()


@pytest.mark.proves("an-unusable-schema-is-drawn-as-unresolved-and-refuses-the-binding")
def test_the_binding_refuses_the_snapshot_the_preview_keeps_drawing() -> None:
    """One finding, two callers, and neither invents its own answer."""
    prose = PublishedRevision(RevisionKind.SCHEMA, b"the verdict a panel returns")
    document = one_node_document(prose)
    registry = ManyRevisionRegistry((SETTLE, prose))
    revision_hash = WorkflowRevisionHash.of(b"one settled verdict")

    with pytest.raises(ReferenceResolutionRefused) as refused:
        bind_run_configuration(
            revision_hash,
            document,
            SubworkflowBinding(),
            AgentBindingSetHash.of(b"no agent roles"),
            registry,
        )

    assert (
        refused.value.refusal.reason is ReferenceRefusalReason.UNUSABLE_SCHEMA_DOCUMENT
    )

    composed = compose_preview(
        revision_hash,
        document,
        SubworkflowBinding(),
        (),
        PublishedSkills(),
        AttestedCapabilities(),
        registry,
        ConfigurationBinding.BOUND,
    )

    assert [
        (entry.reason, entry.site.node, entry.site.field)
        for entry in composed.graph.unresolved_references
    ] == [(ReferenceRefusalReason.UNUSABLE_SCHEMA_DOCUMENT, "decide", "outputs.schema")]
    assert [node.id for node in composed.graph.nodes] == ["decide"]


@pytest.fixture
def counted_retrievals(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Every retrieval the production path attempts, in order."""
    attempted: list[str] = []
    original: Callable[[str], NoReturn] = schemas_v3.refuse_retrieval

    def counting(uri: str) -> NoReturn:
        attempted.append(uri)
        original(uri)

    monkeypatch.setattr(schemas_v3, "refuse_retrieval", counting)
    yield attempted


@pytest.mark.proves("no-schema-reference-ever-leaves-the-document")
def test_no_schema_document_ever_reaches_the_retrieval_seam(
    counted_retrievals: list[str],
) -> None:
    """Reading every document in this suite attempts exactly zero retrievals."""
    documents = [document for _, document, _ in THE_FIVE_THAT_BOUND_CLEANLY]
    documents += [A_REAL_SCHEMA, A_LOCAL_REFERENCE, larger_than_allowed()]

    for document in documents:
        read_schema_document(document)

    assert counted_retrievals == []


@pytest.mark.proves("no-schema-reference-ever-leaves-the-document")
def test_the_only_retrieval_path_raises_instead_of_fetching() -> None:
    """The seam is armed, not absent: any URI at all is a loud failure."""
    with pytest.raises(SchemaRetrievalAttempted):
        refuse_retrieval("https://example.com/verdict.json")

    with pytest.raises(Unretrievable) as refused:
        schema_registry().get_or_retrieve("https://example.com/verdict.json")

    assert isinstance(refused.value.__cause__, SchemaRetrievalAttempted)
