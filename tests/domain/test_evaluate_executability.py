"""One evaluation, every asker: the start, the reads and the publication share it."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.application.evaluate_executability import (
    DocumentNotExecutable,
    ExecutableDocument,
    evaluate_executability,
    public_reason,
)
from atelier2.application.refusals import DurableStateCorrupt, ReadUnavailable
from atelier2.contracts.revisions_v3 import (
    PublishedRevision,
    PublishedRevisionHash,
    RevisionKind,
)
from atelier2.contracts.run_configuration_v3 import ReferenceRefusalReason
from atelier2.contracts.tool_grants_v3 import ToolGrantCapability
from atelier2.contracts.workflows_v3 import what_a_v3_document_still_waits_for
from atelier2.ports.durable_runs import DurableStateCorrupt as PortDurableStateCorrupt
from atelier2.ports.published_revisions import (
    PublishedRevisionFound,
    PublishedRevisionMissing,
    PublishedRevisionsUnavailable,
    ResolvePublishedRevisionResult,
)
from tests.scenarios.workflows import ANY_JSON_SCHEMA, V3_DOCUMENT, declared_output

V1_DOCUMENT = b"""format_version: 1
start: final
nodes:
  - {id: final, type: subworkflow, operation: add, operands: [1, 2], next: null}
"""
GRANT = PublishedRevision(
    RevisionKind.TOOL,
    json.dumps(
        {"capability": ToolGrantCapability.RUN_PROJECT_VERIFICATION.value}
    ).encode(),
)
NOT_A_GRANT = PublishedRevision(RevisionKind.TOOL, b"not even json")


def one_agent(tool: PublishedRevision | None = None) -> bytes:
    tools = (
        ""
        if tool is None
        else f"    tools:\n      - {{ref: verify, revision: {tool.revision_hash.value}}}\n"
    )
    return (
        b"""format_version: 3
name: One agent
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Do the one thing this chain is for.
"""
        + tools.encode()
        + declared_output()
    )


@dataclass(frozen=True)
class Registry:
    """Published revisions by identity, or one answer the store gives instead."""

    published: tuple[PublishedRevision, ...] = ()
    instead: ResolvePublishedRevisionResult | None = None
    asked: list[tuple[RevisionKind, str]] = field(default_factory=list)

    def resolve(
        self, kind: RevisionKind, revision_hash: PublishedRevisionHash
    ) -> ResolvePublishedRevisionResult:
        self.asked.append((kind, revision_hash.value))
        if self.instead is not None:
            return self.instead
        for revision in self.published:
            if revision.kind is kind and revision.revision_hash == revision_hash:
                return PublishedRevisionFound(revision)
        return PublishedRevisionMissing()


def test_a_v1_document_is_executable_without_asking_the_registry() -> None:
    registry = Registry()

    evaluated = evaluate_executability(parse_workflow_document(V1_DOCUMENT), registry)

    assert evaluated == ExecutableDocument()
    assert registry.asked == []


def test_a_form_nothing_binds_is_refused_before_any_reference_is_asked() -> None:
    graph = parse_workflow_document(V3_DOCUMENT)
    registry = Registry()

    evaluated = evaluate_executability(graph, registry)

    assert isinstance(graph.__class__, type)
    assert evaluated == DocumentNotExecutable(what_a_v3_document_still_waits_for(graph))  # type: ignore[arg-type]
    assert registry.asked == []


def test_every_reference_resolved_is_the_snapshot_a_start_freezes() -> None:
    registry = Registry((ANY_JSON_SCHEMA, GRANT))

    evaluated = evaluate_executability(
        parse_workflow_document(one_agent(GRANT)), registry
    )

    assert isinstance(evaluated, ExecutableDocument), evaluated
    assert sorted(
        entry.revision_hash.value for entry in evaluated.resolutions
    ) == sorted([ANY_JSON_SCHEMA.revision_hash.value, GRANT.revision_hash.value])


def test_the_first_unpublished_reference_names_its_site_hash_and_token() -> None:
    evaluated = evaluate_executability(
        parse_workflow_document(one_agent(GRANT)), Registry((ANY_JSON_SCHEMA,))
    )

    assert isinstance(evaluated, DocumentNotExecutable), evaluated
    assert evaluated.refusal is not None
    assert evaluated.refusal.reason is ReferenceRefusalReason.UNPUBLISHED_REVISION
    assert evaluated.reason == public_reason(evaluated.refusal)
    assert "node 'implement' field 'tools'" in evaluated.reason
    assert f"verify@{GRANT.revision_hash.value}" in evaluated.reason
    assert evaluated.reason.endswith("[unpublished_revision]")


def test_the_public_reason_keeps_the_parser_out_of_the_authors_sentence() -> None:
    """The refusal's detail may quote a parser; the listing says the token instead."""
    evaluated = evaluate_executability(
        parse_workflow_document(one_agent(NOT_A_GRANT)),
        Registry((ANY_JSON_SCHEMA, NOT_A_GRANT)),
    )

    assert isinstance(evaluated, DocumentNotExecutable), evaluated
    assert evaluated.refusal is not None
    assert evaluated.refusal.reason is ReferenceRefusalReason.UNREDEEMABLE_TOOL_GRANT
    assert evaluated.refusal.detail not in evaluated.reason
    assert evaluated.reason.endswith(
        "the published revision is not a tool grant this runtime redeems "
        "[unredeemable_tool_grant]"
    )


@pytest.mark.parametrize(
    ("instead", "expected"),
    [
        (
            PublishedRevisionsUnavailable("registry asleep"),
            ReadUnavailable("registry asleep"),
        ),
        (PortDurableStateCorrupt(), DurableStateCorrupt()),
    ],
    ids=["unavailable", "corrupt"],
)
def test_a_registry_that_cannot_answer_is_neither_executable_nor_refused(
    instead: ResolvePublishedRevisionResult, expected: object
) -> None:
    evaluated = evaluate_executability(
        parse_workflow_document(one_agent()), Registry(instead=instead)
    )

    assert evaluated == expected
