"""One evaluation, every asker: the start, the reads and the publication share it."""

from __future__ import annotations

import json
from dataclasses import dataclass

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
PUSH_OPERATION = PublishedRevision(
    RevisionKind.ADAPTER_OPERATION,
    json.dumps(
        {
            "operation": "push-atelier-commit",
            "author": {"name": "Atelier Agent", "email": "agent@example.test"},
            "committer": {"name": "Atelier Core", "email": "core@example.test"},
        }
    ).encode(),
)
PUSH_GRANT = PublishedRevision(
    RevisionKind.TOOL,
    json.dumps(
        {
            "capability": ToolGrantCapability.PUSH_ATELIER_COMMIT.value,
            "operation": {
                "ref": "push-atelier-commit",
                "revision": PUSH_OPERATION.revision_hash.value,
            },
        }
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

    def resolve(
        self, kind: RevisionKind, revision_hash: PublishedRevisionHash
    ) -> ResolvePublishedRevisionResult:
        if self.instead is not None:
            return self.instead
        for revision in self.published:
            if revision.kind is kind and revision.revision_hash == revision_hash:
                return PublishedRevisionFound(revision)
        return PublishedRevisionMissing()


class RegistryNobodyMayAsk:
    """A registry the evaluation must not reach: the verdict is settled before it."""

    def resolve(
        self, kind: RevisionKind, revision_hash: PublishedRevisionHash
    ) -> ResolvePublishedRevisionResult:
        raise AssertionError(f"the evaluation asked the registry for {kind.value}")


def test_a_v1_document_is_executable_without_asking_the_registry() -> None:
    evaluated = evaluate_executability(
        parse_workflow_document(V1_DOCUMENT), RegistryNobodyMayAsk()
    )

    assert evaluated == ExecutableDocument()


def test_a_form_nothing_binds_is_refused_before_any_reference_is_asked() -> None:
    """V3_DOCUMENT declares a graph output nothing carries out of a run."""
    evaluated = evaluate_executability(
        parse_workflow_document(V3_DOCUMENT), RegistryNobodyMayAsk()
    )

    assert isinstance(evaluated, DocumentNotExecutable), evaluated
    assert evaluated.refusal is None
    assert evaluated.reason == "graph outputs nothing carries out of a run: verdict"


def test_every_reference_resolved_is_the_snapshot_a_start_freezes() -> None:
    registry = Registry((ANY_JSON_SCHEMA, GRANT))

    evaluated = evaluate_executability(
        parse_workflow_document(one_agent(GRANT)), registry
    )

    assert isinstance(evaluated, ExecutableDocument), evaluated
    assert sorted(
        entry.revision_hash.value for entry in evaluated.resolutions
    ) == sorted([ANY_JSON_SCHEMA.revision_hash.value, GRANT.revision_hash.value])


def test_push_grant_resolves_and_freezes_its_transitive_operation_pin() -> None:
    evaluated = evaluate_executability(
        parse_workflow_document(one_agent(PUSH_GRANT)),
        Registry((ANY_JSON_SCHEMA, PUSH_GRANT, PUSH_OPERATION)),
    )

    assert isinstance(evaluated, ExecutableDocument), evaluated
    push = next(
        entry
        for entry in evaluated.resolutions
        if entry.kind is RevisionKind.ADAPTER_OPERATION
    )
    assert push.revision_hash == PUSH_OPERATION.revision_hash
    assert push.site.chain == (
        next(
            entry.reference
            for entry in evaluated.resolutions
            if entry.kind is RevisionKind.TOOL
        ),
    )


def test_push_grant_refuses_before_start_when_its_operation_pin_is_missing() -> None:
    evaluated = evaluate_executability(
        parse_workflow_document(one_agent(PUSH_GRANT)),
        Registry((ANY_JSON_SCHEMA, PUSH_GRANT)),
    )

    assert isinstance(evaluated, DocumentNotExecutable), evaluated
    assert evaluated.refusal is not None
    assert evaluated.refusal.kind is RevisionKind.ADAPTER_OPERATION
    assert evaluated.refusal.reason is ReferenceRefusalReason.UNPUBLISHED_REVISION
    assert PUSH_GRANT.revision_hash.value in str(evaluated.refusal.site)


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
