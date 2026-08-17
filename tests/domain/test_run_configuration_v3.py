from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

import pytest

from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.application.bind_run_configuration import bind_run_configuration
from atelier2.application.bind_subworkflow_boundaries import (
    bind_subworkflow_boundaries,
)
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevisionHash,
    AgentRole,
)
from atelier2.contracts.budgets_v3 import BudgetField
from atelier2.contracts.revisions_v3 import (
    PublishedRevision,
    PublishedRevisionHash,
    RevisionKind,
)
from atelier2.contracts.run_configuration_v3 import (
    ReferenceRefusalReason,
    ReferenceResolutionRefused,
    ReferenceSite,
    ResolvedReference,
    RunConfigurationRevision,
    declared_references,
)
from atelier2.contracts.runs import WorkflowRevision
from atelier2.contracts.tool_grants_v3 import ToolGrantCapability
from atelier2.contracts.workflow_bindings_v3 import SubworkflowBinding
from atelier2.contracts.workflows_v3 import VersionedReference, WorkflowGraphV3
from atelier2.ports.published_revisions import (
    PublishedRevisionFound,
    PublishedRevisionMissing,
    PublishRevisionResult,
    ResolvePublishedRevisionResult,
)
from atelier2.ports.workflow_revisions import (
    PublishedWorkflowFound,
    PublishedWorkflowMissing,
    ResolvePublishedWorkflowResult,
)


def published(kind: RevisionKind, body: str) -> PublishedRevision:
    return PublishedRevision(kind, body.encode("utf-8"))


def published_schema(title: str) -> PublishedRevision:
    """A real Draft 2020-12 schema, titled by the thing it describes.

    Prose published under the name `schema` is refused at the reference site, so
    a fixture standing in for a schema has to be one.
    """
    return published(
        RevisionKind.SCHEMA, json.dumps({"title": title, "type": "object"})
    )


SCHEMA_CANDIDATE = published_schema("the workspace a builder produced")
SCHEMA_VERDICT = published_schema("the verdict a review panel returns")
SCHEMA_RECEIPT = published_schema("the receipt a comment leaves behind")
PROFILE = published(RevisionKind.PROFILE, "the standing method of a builder")
SKILL = published(RevisionKind.SKILL, "workspace discipline, and its tool grants")


def published_grant(capability: ToolGrantCapability) -> PublishedRevision:
    """A real tool grant, because prose published under `tool` is refused now.

    A `tools` reference reads the grant it pins exactly as an `outputs.schema`
    reference reads its schema, so a fixture standing in for one has to be one.
    A grant a skill only carries never passes that reader, and stays prose.
    """
    return published(RevisionKind.TOOL, json.dumps({"capability": capability.value}))


TOOL = published_grant(ToolGrantCapability.RUN_PROJECT_VERIFICATION)
POLICY = published(RevisionKind.POLICY, "the house rules of this workshop")
BUDGET = published(
    RevisionKind.BUDGET_POLICY,
    # Prose published under `budget_policy` is refused now, for the same reason
    # prose published under `tool` is: the resolution reads what it pins.
    json.dumps({BudgetField.ATTEMPT_DEADLINE_SECONDS.value: 900}),
)
RETRY = published(RevisionKind.RETRY_POLICY, "twice, then the node has failed")
CANCELLATION = published(RevisionKind.CANCELLATION_POLICY, "drain, never abandon")
CONTEXT_SOURCE = published(RevisionKind.CONTEXT_SOURCE, "the requirement of record")
READ_OPERATION = published(RevisionKind.READ_OPERATION, "search, bounded and receipted")
DETERMINISTIC = published(
    RevisionKind.DETERMINISTIC_OPERATION, "decide what the panel returned"
)
CHILD_DETERMINISTIC = published(
    RevisionKind.DETERMINISTIC_OPERATION, "merge every review verdict"
)
ADAPTER = published(RevisionKind.ADAPTER_OPERATION, "comment on the requirement")

REGISTRY_CONTENTS = (
    SCHEMA_CANDIDATE,
    SCHEMA_VERDICT,
    SCHEMA_RECEIPT,
    PROFILE,
    SKILL,
    TOOL,
    POLICY,
    BUDGET,
    RETRY,
    CANCELLATION,
    CONTEXT_SOURCE,
    READ_OPERATION,
    DETERMINISTIC,
    CHILD_DETERMINISTIC,
    ADAPTER,
)

PARENT_TEMPLATE = """format_version: 3
name: Build, review in a panel, settle the verdict and hand it off
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Build every acceptance sentence of the bound story.
    profile: {{ref: builder_method, revision: {profile}}}
    skills:
      - {{ref: workspace_discipline, revision: {skill}}}
    tools:
      - {{ref: repository_write, revision: {tool}}}
    policy: {{ref: house_rules, revision: {policy}}}
    budget: {{ref: build_budget, revision: {budget}}}
    retry: {{ref: twice, revision: {retry}}}
    cancellation: {{ref: drain, revision: {cancellation}}}
    required_context:
      - name: story
        source:
          ref: requirement
          revision: {source}
          selector: story_acceptance
    available_context:
      - name: decisions
        source: {{ref: decision_record_index, revision: {source}}}
        read_operations:
          - {{ref: search, revision: {read}}}
    outputs:
      - name: candidate
        schema: {{ref: workspace_candidate, revision: {candidate}}}
  - id: panel
    type: subworkflow
    depends_on: [implement]
    workflow: {{ref: review_panel, revision: {child}}}
    budget: {{ref: build_budget, revision: {budget}}}
    inputs:
      - name: candidate
        from: {{node: implement, output: candidate}}
    outputs:
      - name: verdict
        schema: {{ref: review_verdict, revision: {verdict}}}
  - id: decide
    type: deterministic
    depends_on: [panel]
    operation: {{ref: settle_the_panel, revision: {deterministic}}}
    retry: {{ref: twice, revision: {retry}}}
    inputs:
      - name: verdict
        from: {{node: panel, output: verdict}}
    outputs:
      - name: verdict
        schema: {{ref: review_verdict, revision: {verdict}}}
  - id: hand_off
    type: action
    depends_on: [decide]
    operation: {{ref: requirement_comment, revision: {adapter}}}
    inputs:
      - name: verdict
        from: {{node: decide, output: verdict}}
    outputs:
      - name: receipt
        schema: {{ref: comment_receipt, revision: {receipt}}}
"""

CHILD_TEMPLATE = """format_version: 3
name: Merge the panel's review verdicts into one
graph_inputs:
  - name: candidate
    schema: {{ref: workspace_candidate, revision: {candidate}}}
graph_outputs:
  - name: verdict
    from: {{node: merge, output: merged}}
nodes:
  - id: merge
    type: deterministic
    operation: {{ref: merge_review_verdicts, revision: {deterministic}}}
    inputs:
      - name: candidate
        from: {{graph_input: candidate}}
    outputs:
      - name: merged
        schema: {{ref: review_verdict, revision: {verdict}}}
"""

CHILD = CHILD_TEMPLATE.format(
    candidate=SCHEMA_CANDIDATE.revision_hash.value,
    deterministic=CHILD_DETERMINISTIC.revision_hash.value,
    verdict=SCHEMA_VERDICT.revision_hash.value,
).encode("utf-8")

CHILD_REVISION = WorkflowRevision(CHILD).revision_hash.value
CHILD_REFERENCE = ("review_panel", CHILD_REVISION)
CHILD_CHAIN = (VersionedReference(ref="review_panel", revision=CHILD_REVISION),)

PARENT = PARENT_TEMPLATE.format(
    profile=PROFILE.revision_hash.value,
    skill=SKILL.revision_hash.value,
    tool=TOOL.revision_hash.value,
    policy=POLICY.revision_hash.value,
    budget=BUDGET.revision_hash.value,
    retry=RETRY.revision_hash.value,
    cancellation=CANCELLATION.revision_hash.value,
    source=CONTEXT_SOURCE.revision_hash.value,
    read=READ_OPERATION.revision_hash.value,
    candidate=SCHEMA_CANDIDATE.revision_hash.value,
    child=CHILD_REVISION,
    verdict=SCHEMA_VERDICT.revision_hash.value,
    deterministic=DETERMINISTIC.revision_hash.value,
    adapter=ADAPTER.revision_hash.value,
    receipt=SCHEMA_RECEIPT.revision_hash.value,
).encode("utf-8")

ROLE_MATRIX = AgentBindingSet(
    (
        AgentBinding(
            AgentRole("builder"),
            AgentConfigurationRevisionHash.of(b"the builder configuration"),
        ),
    )
)

type RegistryAnswers = Mapping[tuple[RevisionKind, str], PublishedRevision]

FULL_REGISTRY: RegistryAnswers = MappingProxyType(
    {
        (revision.kind, revision.revision_hash.value): revision
        for revision in REGISTRY_CONTENTS
    }
)


@dataclass
class PublishedRegistry:
    """The registries as this binding reads them: what is asked, and what answers.

    It records every lookup, so a test can prove which kinds a binding asks about.
    """

    answers: RegistryAnswers
    asked: list[tuple[RevisionKind, str]] = field(default_factory=list)

    def resolve(
        self, kind: RevisionKind, revision_hash: PublishedRevisionHash
    ) -> ResolvePublishedRevisionResult:
        self.asked.append((kind, revision_hash.value))
        revision = self.answers.get((kind, revision_hash.value))
        if revision is None:
            return PublishedRevisionMissing()
        return PublishedRevisionFound(revision)

    def publish_revision(self, revision: PublishedRevision) -> PublishRevisionResult:
        raise NotImplementedError("binding a run configuration publishes nothing")


@dataclass(frozen=True)
class PublishedWorkflows:
    documents: Mapping[tuple[str, str], bytes]

    def resolve(self, reference: VersionedReference) -> ResolvePublishedWorkflowResult:
        document = self.documents.get((reference.ref, reference.revision))
        if document is None:
            return PublishedWorkflowMissing()
        return PublishedWorkflowFound(WorkflowRevision(document))


def parsed(document: bytes = PARENT) -> WorkflowGraphV3:
    graph = parse_workflow_document(document)
    assert isinstance(graph, WorkflowGraphV3)
    return graph


MAXIMUM_ITERATION_ROUNDS = 8


def bound_children(document: bytes = PARENT) -> SubworkflowBinding:
    return bind_subworkflow_boundaries(
        parsed(document),
        PublishedWorkflows({CHILD_REFERENCE: CHILD}),
        parse_workflow_document,
        2,
        MAXIMUM_ITERATION_ROUNDS,
    )


def bind(
    document: bytes = PARENT,
    answers: RegistryAnswers = FULL_REGISTRY,
    role_matrix: AgentBindingSet = ROLE_MATRIX,
    registry: PublishedRegistry | None = None,
    binding: SubworkflowBinding | None = None,
) -> RunConfigurationRevision:
    return bind_run_configuration(
        WorkflowRevision(document).revision_hash,
        parsed(document),
        binding if binding is not None else bound_children(document),
        role_matrix.binding_set_hash,
        registry if registry is not None else PublishedRegistry(answers),
    )


def without_publication(*absent: PublishedRevision) -> RegistryAnswers:
    withdrawn = {(revision.kind, revision.revision_hash.value) for revision in absent}
    return {
        key: revision for key, revision in FULL_REGISTRY.items() if key not in withdrawn
    }


DECLARED_PARENT_REFERENCES = (
    (RevisionKind.PROFILE, "implement", "profile", PROFILE),
    (RevisionKind.SKILL, "implement", "skills", SKILL),
    (RevisionKind.TOOL, "implement", "tools", TOOL),
    (RevisionKind.POLICY, "implement", "policy", POLICY),
    (RevisionKind.BUDGET_POLICY, "implement", "budget", BUDGET),
    (RevisionKind.RETRY_POLICY, "implement", "retry", RETRY),
    (RevisionKind.CANCELLATION_POLICY, "implement", "cancellation", CANCELLATION),
    (
        RevisionKind.CONTEXT_SOURCE,
        "implement",
        "required_context.source",
        CONTEXT_SOURCE,
    ),
    (
        RevisionKind.CONTEXT_SOURCE,
        "implement",
        "available_context.source",
        CONTEXT_SOURCE,
    ),
    (
        RevisionKind.READ_OPERATION,
        "implement",
        "available_context.read_operations",
        READ_OPERATION,
    ),
    (RevisionKind.SCHEMA, "implement", "outputs.schema", SCHEMA_CANDIDATE),
    (RevisionKind.BUDGET_POLICY, "panel", "budget", BUDGET),
    (RevisionKind.SCHEMA, "panel", "outputs.schema", SCHEMA_VERDICT),
    (RevisionKind.DETERMINISTIC_OPERATION, "decide", "operation", DETERMINISTIC),
    (RevisionKind.RETRY_POLICY, "decide", "retry", RETRY),
    (RevisionKind.SCHEMA, "decide", "outputs.schema", SCHEMA_VERDICT),
    (RevisionKind.ADAPTER_OPERATION, "hand_off", "operation", ADAPTER),
    (RevisionKind.SCHEMA, "hand_off", "outputs.schema", SCHEMA_RECEIPT),
    (
        RevisionKind.WORKFLOW,
        "panel",
        "workflow",
        PublishedRevision(RevisionKind.WORKFLOW, CHILD),
    ),
)


def test_a_published_revision_is_identified_by_the_exact_bytes_it_carries() -> None:
    body = json.dumps(
        {"title": "the workspace a builder produced", "type": "object"}
    ).encode("utf-8")

    assert SCHEMA_CANDIDATE.revision_hash == PublishedRevisionHash.of(body)
    assert (
        PublishedRevision(RevisionKind.PROFILE, body).revision_hash
        == SCHEMA_CANDIDATE.revision_hash
    )


def test_a_published_workflow_carries_one_hash_under_either_reading() -> None:
    assert (
        PublishedRevision(RevisionKind.WORKFLOW, CHILD).revision_hash.value
        == WorkflowRevision(CHILD).revision_hash.value
    )


def test_a_document_declares_the_reference_its_vocabulary_puts_at_each_site() -> None:
    declared = declared_references(parsed())

    assert {
        (entry.kind, entry.site.node, entry.site.field, entry.reference.revision)
        for entry in declared
    } == {
        (kind, node, field_name, revision.revision_hash.value)
        for kind, node, field_name, revision in DECLARED_PARENT_REFERENCES
    }


def test_a_workflow_reference_binds_the_child_its_binder_resolved_without_a_registry() -> (
    None
):
    registry = PublishedRegistry(FULL_REGISTRY)

    configuration = bind(registry=registry)

    bound = [
        entry
        for entry in configuration.resolutions
        if entry.kind is RevisionKind.WORKFLOW
    ]
    assert [
        (entry.site.node, entry.site.field, entry.revision_hash.value)
        for entry in bound
    ] == [("panel", "workflow", CHILD_REVISION)]
    assert not any(kind is RevisionKind.WORKFLOW for kind, _ in registry.asked)


def test_a_workflow_reference_no_bound_child_carries_is_refused() -> None:
    with pytest.raises(ReferenceResolutionRefused) as raised:
        bind(binding=SubworkflowBinding())

    refusal = raised.value.refusal
    assert refusal.reason is ReferenceRefusalReason.UNBOUND_WORKFLOW_REFERENCE
    assert (refusal.site.node, refusal.site.field) == ("panel", "workflow")


def test_the_snapshot_binds_every_reference_to_the_revision_it_resolved_to() -> None:
    configuration = bind()

    assert {
        (entry.kind, entry.site.node, entry.site.field, entry.revision_hash.value)
        for entry in configuration.resolutions
        if not entry.site.chain
    } == {
        (kind, node, field_name, revision.revision_hash.value)
        for kind, node, field_name, revision in DECLARED_PARENT_REFERENCES
    }


def test_a_registry_answering_with_another_revision_refuses_the_whole_snapshot() -> (
    None
):
    other_bytes = published_schema("a verdict shaped differently")
    contradicting = dict(FULL_REGISTRY) | {
        (RevisionKind.SCHEMA, SCHEMA_VERDICT.revision_hash.value): other_bytes
    }

    with pytest.raises(ReferenceResolutionRefused) as raised:
        bind(answers=contradicting)

    refusal = raised.value.refusal
    assert refusal.reason is ReferenceRefusalReason.RESOLVED_REVISION_MISMATCH
    assert other_bytes.revision_hash.value in str(refusal)


def test_the_snapshot_binds_the_role_matrix_and_the_workflow_it_configures() -> None:
    configuration = bind()

    assert configuration.binding_set_hash == ROLE_MATRIX.binding_set_hash
    assert (
        configuration.workflow_revision_hash == WorkflowRevision(PARENT).revision_hash
    )


def test_the_snapshot_binds_every_subworkflow_to_its_exact_child_revision() -> None:
    configuration = bind()

    assert [
        (entry.site.node, entry.reference.revision, entry.revision_hash.value)
        for entry in configuration.resolutions
        if entry.kind is RevisionKind.WORKFLOW
    ] == [("panel", CHILD_REVISION, CHILD_REVISION)]


def test_a_child_reference_enters_the_snapshot_through_the_chain_it_was_reached_by() -> (
    None
):
    configuration = bind()

    reached = [entry for entry in configuration.resolutions if entry.site.chain]
    assert all(entry.site.chain == CHILD_CHAIN for entry in reached)
    assert {(entry.kind, entry.site.node, entry.site.field) for entry in reached} == {
        (RevisionKind.SCHEMA, None, "graph_inputs.schema"),
        (RevisionKind.SCHEMA, "merge", "outputs.schema"),
        (RevisionKind.DETERMINISTIC_OPERATION, "merge", "operation"),
    }


def test_one_matrix_reached_in_any_order_is_one_immutable_revision() -> None:
    configuration = bind()

    reversed_entries = RunConfigurationRevision(
        configuration.workflow_revision_hash,
        configuration.binding_set_hash,
        tuple(reversed(configuration.resolutions)),
    )
    assert reversed_entries.revision_hash == configuration.revision_hash
    assert reversed_entries.resolutions == configuration.resolutions


def test_a_matrix_binding_one_other_revision_is_another_run_configuration() -> None:
    configuration = bind()
    rebound = RunConfigurationRevision(
        configuration.workflow_revision_hash,
        configuration.binding_set_hash,
        configuration.resolutions[1:]
        + (
            ResolvedReference(
                configuration.resolutions[0].site,
                configuration.resolutions[0].kind,
                configuration.resolutions[0].reference,
                PublishedRevisionHash.of(b"a revision nobody else bound"),
            ),
        ),
    )

    assert rebound.revision_hash != configuration.revision_hash


def test_a_matrix_binding_one_other_child_is_another_run_configuration() -> None:
    configuration = bind()
    without_the_child = tuple(
        entry
        for entry in configuration.resolutions
        if entry.kind is not RevisionKind.WORKFLOW
    )
    rebound = RunConfigurationRevision(
        configuration.workflow_revision_hash,
        configuration.binding_set_hash,
        without_the_child,
    )

    assert rebound.revision_hash != configuration.revision_hash


def test_a_different_role_matrix_is_a_different_run_configuration() -> None:
    other_matrix = AgentBindingSet(
        (
            AgentBinding(
                AgentRole("reviewer"),
                AgentConfigurationRevisionHash.of(b"the reviewer configuration"),
            ),
        )
    )

    assert bind(role_matrix=other_matrix).revision_hash != bind().revision_hash


@dataclass(frozen=True)
class Refused:
    reason: ReferenceRefusalReason
    node: str | None
    field: str
    reference: str
    document: bytes = PARENT
    answers: RegistryAnswers = FULL_REGISTRY


UNKNOWN_HASH = PublishedRevisionHash.of(b"a revision nobody published").value

REFUSALS: Mapping[str, Refused] = {
    "withdrawn-skill": Refused(
        ReferenceRefusalReason.UNPUBLISHED_REVISION,
        "implement",
        "skills",
        "workspace_discipline",
        answers=without_publication(SKILL),
    ),
    "withdrawn-adapter-operation": Refused(
        ReferenceRefusalReason.UNPUBLISHED_REVISION,
        "hand_off",
        "operation",
        "requirement_comment",
        answers=without_publication(ADAPTER),
    ),
    "hash-nobody-published": Refused(
        ReferenceRefusalReason.UNPUBLISHED_REVISION,
        "implement",
        "profile",
        "builder_method",
        document=PARENT.replace(
            PROFILE.revision_hash.value.encode("ascii"), UNKNOWN_HASH.encode("ascii")
        ),
    ),
    "withdrawn-inside-a-bound-child": Refused(
        ReferenceRefusalReason.UNPUBLISHED_REVISION,
        "merge",
        "operation",
        "merge_review_verdicts",
        answers=without_publication(CHILD_DETERMINISTIC),
    ),
    "revision-of-another-kind": Refused(
        ReferenceRefusalReason.REVISION_KIND_MISMATCH,
        "implement",
        "policy",
        "house_rules",
        answers=FULL_REGISTRY
        | {(RevisionKind.POLICY, POLICY.revision_hash.value): SKILL},
    ),
    "revision-that-is-no-hash": Refused(
        ReferenceRefusalReason.MALFORMED_REVISION,
        "implement",
        "profile",
        "builder_method",
        document=PARENT.replace(
            PROFILE.revision_hash.value.encode("ascii"), b"profile-1"
        ),
    ),
    "revision-shortened-to-a-prefix": Refused(
        ReferenceRefusalReason.MALFORMED_REVISION,
        "implement",
        "cancellation",
        "drain",
        document=PARENT.replace(
            CANCELLATION.revision_hash.value.encode("ascii"),
            CANCELLATION.revision_hash.value[:32].encode("ascii"),
        ),
    ),
    "revision-shouted-in-upper-case": Refused(
        ReferenceRefusalReason.MALFORMED_REVISION,
        "hand_off",
        "operation",
        "requirement_comment",
        document=PARENT.replace(
            ADAPTER.revision_hash.value.encode("ascii"),
            ADAPTER.revision_hash.value.upper().encode("ascii"),
        ),
    ),
}


@pytest.mark.parametrize("case", REFUSALS)
def test_a_reference_that_does_not_resolve_refuses_naming_node_field_reference(
    case: str,
) -> None:
    expected = REFUSALS[case]

    with pytest.raises(ReferenceResolutionRefused) as raised:
        bind(document=expected.document, answers=expected.answers)

    refusal = raised.value.refusal
    assert refusal.reason is expected.reason
    assert (refusal.site.node, refusal.site.field) == (expected.node, expected.field)
    assert refusal.reference.ref == expected.reference
    assert expected.reference in str(refusal)
    assert expected.reason.value in str(refusal)


def test_a_refused_child_reference_names_the_chain_it_was_reached_through() -> None:
    with pytest.raises(ReferenceResolutionRefused) as raised:
        bind(answers=without_publication(CHILD_DETERMINISTIC))

    refusal = raised.value.refusal
    assert refusal.site.chain == CHILD_CHAIN
    assert f"review_panel@{CHILD_REVISION}" in str(refusal)


def test_a_refused_reference_names_the_declared_entry_that_carries_it() -> None:
    with pytest.raises(ReferenceResolutionRefused) as raised:
        bind(answers=without_publication(SCHEMA_RECEIPT))

    refusal = raised.value.refusal
    assert refusal.site == ReferenceSite("outputs.schema", "hand_off", "receipt")
    assert "'receipt'" in str(refusal)
