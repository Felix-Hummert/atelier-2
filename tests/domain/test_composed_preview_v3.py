from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

import pytest

from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.application.compose_preview import compose_preview
from atelier2.contracts.agents import (
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutorRevision,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
    ResolvedAgentBinding,
)
from atelier2.contracts.capabilities_v3 import (
    AttestedCapabilities,
    CapabilityAttestation,
    CapabilityRequirement,
    CapabilitySubject,
    Executable,
    NotExecutable,
    PublishedSkills,
    RuntimeCapability,
    SkillContents,
    SubjectIdentity,
    required_capabilities,
)
from atelier2.contracts.composed_preview_v3 import (
    ComposedPreview,
    ComposedPreviewGraph,
    ConfigurationBinding,
    PreviewEdge,
    PreviewNode,
    PreviewNodeKind,
    PreviewRole,
)
from atelier2.contracts.revisions_v3 import (
    PublishedRevision,
    PublishedRevisionHash,
    RevisionKind,
)
from atelier2.contracts.run_configuration_v3 import ReferenceRefusalReason
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
SCHEMA_OPINION = published_schema("what one reviewer thought of it")
SCHEMA_VERDICT = published_schema("the verdict a review panel returns")
SCHEMA_APPROVAL = published_schema("the word an operator gave")
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
CARRIED_TOOL = published(RevisionKind.TOOL, "the grant that skill brings with it")
CONTEXT_SOURCE = published(RevisionKind.CONTEXT_SOURCE, "the requirement of record")
READ_OPERATION = published(RevisionKind.READ_OPERATION, "search, bounded and receipted")
DETERMINISTIC = published(
    RevisionKind.DETERMINISTIC_OPERATION, "decide what the panel returned"
)
CHILD_DETERMINISTIC = published(
    RevisionKind.DETERMINISTIC_OPERATION, "merge every review opinion"
)
ADAPTER = published(
    RevisionKind.ADAPTER_OPERATION,
    json.dumps({"operation": "open-pr"}),
)

REGISTRY_CONTENTS = (
    SCHEMA_CANDIDATE,
    SCHEMA_OPINION,
    SCHEMA_VERDICT,
    SCHEMA_APPROVAL,
    SCHEMA_RECEIPT,
    PROFILE,
    SKILL,
    TOOL,
    CONTEXT_SOURCE,
    READ_OPERATION,
    DETERMINISTIC,
    CHILD_DETERMINISTIC,
    ADAPTER,
)

PARENT_TEMPLATE = """format_version: 3
name: Build, review in a panel, confirm with the operator and hand off
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
    inputs:
      - name: verdict
        from: {{node: panel, output: verdict}}
    outputs:
      - name: verdict
        schema: {{ref: review_verdict, revision: {verdict}}}
  - id: confirm
    type: wait
    depends_on: [decide]
    prompt: Does this verdict go out to the requirement?
    outputs:
      - name: approval
        schema: {{ref: operator_word, revision: {approval}}}
  - id: hand_off
    type: action
    depends_on: [decide, confirm]
    join: all_succeeded
    operation: {{ref: requirement_comment, revision: {adapter}}}
    inputs:
      - name: verdict
        from: {{node: decide, output: verdict}}
    outputs:
      - name: receipt
        schema: {{ref: comment_receipt, revision: {receipt}}}
"""

CHILD_TEMPLATE = """format_version: 3
name: Read the candidate and merge the panel's opinions
graph_inputs:
  - name: candidate
    schema: {{ref: workspace_candidate, revision: {candidate}}}
graph_outputs:
  - name: verdict
    from: {{node: merge, output: merged}}
nodes:
  - id: read_it
    type: agent
    role: reviewer
    mode: interactive
    instruction: Judge the candidate against the acceptance sentences.
    inputs:
      - name: candidate
        from: {{graph_input: candidate}}
    outputs:
      - name: opinion
        schema: {{ref: review_opinion, revision: {opinion}}}
        confirmed_by: operator
  - id: merge
    type: deterministic
    depends_on: [read_it]
    operation: {{ref: merge_review_opinions, revision: {deterministic}}}
    inputs:
      - name: read
        from: {{node: read_it, output: opinion}}
    outputs:
      - name: merged
        schema: {{ref: review_verdict, revision: {verdict}}}
"""

SKILL_REFERENCE = VersionedReference(
    ref="workspace_discipline", revision=SKILL.revision_hash.value
)
CARRIED_GRANT = VersionedReference(
    ref="workspace_write", revision=CARRIED_TOOL.revision_hash.value
)
TOOL_REFERENCE = VersionedReference(
    ref="repository_write", revision=TOOL.revision_hash.value
)
SKILL_CONTENTS = PublishedSkills(
    (SkillContents(SKILL.revision_hash.value, (CARRIED_GRANT,)),)
)

CHILD = CHILD_TEMPLATE.format(
    candidate=SCHEMA_CANDIDATE.revision_hash.value,
    opinion=SCHEMA_OPINION.revision_hash.value,
    deterministic=CHILD_DETERMINISTIC.revision_hash.value,
    verdict=SCHEMA_VERDICT.revision_hash.value,
).encode("utf-8")

CHILD_REFERENCE = VersionedReference(
    ref="review_panel", revision=WorkflowRevision(CHILD).revision_hash.value
)
CHILD_CHAIN = (CHILD_REFERENCE,)

PARENT = PARENT_TEMPLATE.format(
    profile=PROFILE.revision_hash.value,
    skill=SKILL.revision_hash.value,
    tool=TOOL.revision_hash.value,
    source=CONTEXT_SOURCE.revision_hash.value,
    read=READ_OPERATION.revision_hash.value,
    candidate=SCHEMA_CANDIDATE.revision_hash.value,
    child=CHILD_REFERENCE.revision,
    verdict=SCHEMA_VERDICT.revision_hash.value,
    deterministic=DETERMINISTIC.revision_hash.value,
    approval=SCHEMA_APPROVAL.revision_hash.value,
    adapter=ADAPTER.revision_hash.value,
    receipt=SCHEMA_RECEIPT.revision_hash.value,
).encode("utf-8")

REUSED_PANEL = """  - id: {node}
    type: subworkflow
    depends_on: [implement]
    workflow: {{{{ref: review_panel, revision: {{child}}}}}}
    inputs:
      - name: candidate
        from: {{{{node: implement, output: candidate}}}}
    outputs:
      - name: verdict
        schema: {{{{ref: review_verdict, revision: {{verdict}}}}}}
"""

TWICE_BOUND_TEMPLATE = (
    """format_version: 3
name: Build one candidate and send it to two review panels
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Build the candidate both panels are going to review.
    outputs:
      - name: candidate
        schema: {{ref: workspace_candidate, revision: {candidate}}}
"""
    + REUSED_PANEL.format(node="panel")
    + REUSED_PANEL.format(node="panel_two")
)

TWICE_BOUND = TWICE_BOUND_TEMPLATE.format(
    candidate=SCHEMA_CANDIDATE.revision_hash.value,
    child=CHILD_REFERENCE.revision,
    verdict=SCHEMA_VERDICT.revision_hash.value,
).encode("utf-8")

UNPINNED_PARENT = b"""format_version: 3
name: Build the story against a profile nobody pinned properly
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Build the story against a profile nobody pinned properly.
    profile: {ref: builder_method, revision: the-latest-one}
    outputs:
      - name: candidate
        schema: {ref: workspace_candidate, revision: also-not-a-hash}
"""

type RegistryAnswers = Mapping[tuple[RevisionKind, str], PublishedRevision]

FULL_REGISTRY: RegistryAnswers = MappingProxyType(
    {
        (revision.kind, revision.revision_hash.value): revision
        for revision in REGISTRY_CONTENTS
    }
)


@dataclass(frozen=True)
class PublishedRegistry:
    """The registries as this preview reads them: what is asked, and what answers."""

    answers: RegistryAnswers

    def resolve(
        self, kind: RevisionKind, revision_hash: PublishedRevisionHash
    ) -> ResolvePublishedRevisionResult:
        revision = self.answers.get((kind, revision_hash.value))
        if revision is None:
            return PublishedRevisionMissing()
        return PublishedRevisionFound(revision)

    def publish_revision(self, revision: PublishedRevision) -> PublishRevisionResult:
        raise NotImplementedError("composing a preview publishes nothing")


def _auth() -> AuthProfileRevision:
    return AuthProfileRevision(
        "workshop", 1, ProviderId("anthropic"), AuthMode.SUBSCRIPTION
    )


def _binding(
    role: str,
    model: str,
    capability: AgentExecutionCapability = AgentExecutionCapability.HEADLESS,
) -> ResolvedAgentBinding:
    auth = _auth()
    configuration = AgentConfigurationRevision(
        model,
        auth.revision_hash,
        AgentExecutorRevision("claude-cli/v1"),
        capability,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    return ResolvedAgentBinding(AgentRole(role), configuration, auth)


BUILDER = _binding("builder", "claude-opus-5")
REVIEWER = _binding("reviewer", "claude-sonnet-5", AgentExecutionCapability.INTERACTIVE)
ROLE_MATRIX = (BUILDER, REVIEWER)


def parsed(document: bytes = PARENT) -> WorkflowGraphV3:
    graph = parse_workflow_document(document)
    assert isinstance(graph, WorkflowGraphV3)
    return graph


MAXIMUM_ITERATION_ROUNDS = 8


def demanded(
    document: bytes = PARENT,
    role_matrix: Sequence[ResolvedAgentBinding] = ROLE_MATRIX,
) -> tuple[CapabilityRequirement, ...]:
    return required_capabilities(
        parsed(document), SubworkflowBinding(), tuple(role_matrix), SKILL_CONTENTS
    )


def attesting(
    requirements: Sequence[CapabilityRequirement],
) -> AttestedCapabilities:
    """The exact manifest that proves one requirement set, and nothing more."""
    proven: dict[RuntimeCapability, set[SubjectIdentity]] = {}
    for requirement in requirements:
        subjects = proven.setdefault(requirement.capability, set())
        if requirement.subject is not None:
            subjects.add(requirement.subject.identity)
    return AttestedCapabilities(
        tuple(
            CapabilityAttestation(capability, frozenset(subjects))
            for capability, subjects in proven.items()
        )
    )


FULL_ATTESTATION = attesting(demanded())


def without(
    attested: AttestedCapabilities, capability: RuntimeCapability
) -> AttestedCapabilities:
    return AttestedCapabilities(
        tuple(entry for entry in attested.entries if entry.capability is not capability)
    )


def without_publication(*absent: PublishedRevision) -> RegistryAnswers:
    withdrawn = {(revision.kind, revision.revision_hash.value) for revision in absent}
    return {
        key: revision for key, revision in FULL_REGISTRY.items() if key not in withdrawn
    }


def preview(
    document: bytes = PARENT,
    answers: RegistryAnswers = FULL_REGISTRY,
    role_matrix: Sequence[ResolvedAgentBinding] = ROLE_MATRIX,
    attested: AttestedCapabilities = FULL_ATTESTATION,
    configuration: ConfigurationBinding = ConfigurationBinding.PROPOSED,
    skills: PublishedSkills = SKILL_CONTENTS,
) -> ComposedPreview:
    return compose_preview(
        WorkflowRevision(document).revision_hash,
        parsed(document),
        SubworkflowBinding(),
        tuple(role_matrix),
        skills,
        attested,
        PublishedRegistry(answers),
        configuration,
    )


def node_of(graph: ComposedPreviewGraph, node_id: str) -> PreviewNode:
    for node in graph.nodes:
        if node.id == node_id:
            return node
    raise AssertionError(f"the preview carries no node {node_id!r}")


def demands_of(node: PreviewNode) -> set[RuntimeCapability]:
    return {requirement.capability for requirement in node.demands}


def test_every_node_is_previewed_under_the_kind_the_parser_gave_it() -> None:
    composed = preview()

    assert [(node.id, node.kind) for node in composed.graph.nodes] == [
        ("implement", PreviewNodeKind.AGENT),
        ("panel", PreviewNodeKind.SUBWORKFLOW),
        ("decide", PreviewNodeKind.DETERMINISTIC),
        ("confirm", PreviewNodeKind.WAIT),
        ("hand_off", PreviewNodeKind.ACTION),
    ]


def test_the_previewed_kinds_are_the_vocabulary_the_document_writes() -> None:
    written = {node.type for node in parsed().nodes} | {
        node.type for node in parsed(CHILD).nodes
    }

    assert written == {kind.value for kind in PreviewNodeKind}


def test_an_agent_node_carries_the_role_provider_model_and_mode_it_is_bound_to() -> (
    None
):
    composed = preview()

    implement = node_of(composed.graph, "implement")
    assert implement.role == PreviewRole(
        "builder",
        "anthropic",
        "claude-opus-5",
        BUILDER.configuration.revision_hash.value,
    )
    assert implement.mode == "headless"


def test_a_node_that_binds_no_role_previews_neither_a_role_nor_a_mode() -> None:
    composed = preview()

    assert [
        (node.id, node.role, node.mode)
        for node in composed.graph.nodes
        if node.kind is not PreviewNodeKind.AGENT
    ] == [
        ("panel", None, None),
        ("decide", None, None),
        ("confirm", None, None),
        ("hand_off", None, None),
    ]


def test_the_preview_carries_the_dependency_edges_the_document_declares() -> None:
    composed = preview()

    assert composed.graph.edges == (
        PreviewEdge("implement", "panel"),
        PreviewEdge("panel", "decide"),
        PreviewEdge("decide", "confirm"),
        PreviewEdge("decide", "hand_off"),
        PreviewEdge("confirm", "hand_off"),
    )


def test_every_node_previews_the_join_a_scheduler_really_applies() -> None:
    composed = preview()

    assert [(node.id, node.join) for node in composed.graph.nodes] == [
        ("implement", None),
        ("panel", "all_succeeded"),
        ("decide", "all_succeeded"),
        ("confirm", "all_succeeded"),
        ("hand_off", "all_succeeded"),
    ]


@pytest.mark.parametrize(
    ("node_id", "capabilities"),
    (
        (
            "implement",
            {
                RuntimeCapability.AGENT_EXECUTION,
                RuntimeCapability.OUTPUT_VALIDATION,
                RuntimeCapability.CONTEXT_MATERIALIZATION,
                RuntimeCapability.CONTEXT_RESOLUTION,
                RuntimeCapability.SKILL_INSTALLATION,
                RuntimeCapability.TOOL_GRANTS,
            },
        ),
        ("panel", set()),
        (
            "decide",
            {
                RuntimeCapability.DETERMINISTIC_OPERATIONS,
                RuntimeCapability.DAG_SCHEDULING,
            },
        ),
        ("confirm", set()),
        (
            "hand_off",
            {RuntimeCapability.EXTERNAL_EFFECTS, RuntimeCapability.DAG_SCHEDULING},
        ),
    ),
)
def test_a_node_carries_exactly_the_capabilities_its_own_form_demands(
    node_id: str, capabilities: set[RuntimeCapability]
) -> None:
    assert demands_of(node_of(preview().graph, node_id)) == capabilities


def test_a_grant_a_skill_carries_lands_on_the_node_that_installs_the_skill() -> None:
    implement = node_of(preview().graph, "implement")

    assert [
        (requirement.site.chain, requirement.subject)
        for requirement in implement.demands
        if requirement.capability is RuntimeCapability.TOOL_GRANTS
        and requirement.site.chain
    ] == [
        (
            (SKILL_REFERENCE,),
            CapabilitySubject.of(RevisionKind.TOOL, CARRIED_GRANT),
        )
    ]


def test_every_demand_of_the_closure_lands_on_exactly_one_node() -> None:
    composed = preview()

    landed = [
        requirement for node in composed.graph.nodes for requirement in node.demands
    ]

    assert sorted(map(str, landed)) == sorted(map(str, demanded()))


def test_every_reference_previews_the_published_revision_it_lands_in() -> None:
    composed = preview()

    assert {
        (entry.site.node, entry.site.field, entry.kind, entry.revision_hash.value)
        for entry in composed.graph.resolved_references
    } == {
        ("implement", "profile", RevisionKind.PROFILE, PROFILE.revision_hash.value),
        ("implement", "skills", RevisionKind.SKILL, SKILL.revision_hash.value),
        ("implement", "tools", RevisionKind.TOOL, TOOL.revision_hash.value),
        (
            "implement",
            "required_context.source",
            RevisionKind.CONTEXT_SOURCE,
            CONTEXT_SOURCE.revision_hash.value,
        ),
        (
            "implement",
            "available_context.source",
            RevisionKind.CONTEXT_SOURCE,
            CONTEXT_SOURCE.revision_hash.value,
        ),
        (
            "implement",
            "available_context.read_operations",
            RevisionKind.READ_OPERATION,
            READ_OPERATION.revision_hash.value,
        ),
        (
            "implement",
            "outputs.schema",
            RevisionKind.SCHEMA,
            SCHEMA_CANDIDATE.revision_hash.value,
        ),
        (
            "panel",
            "outputs.schema",
            RevisionKind.SCHEMA,
            SCHEMA_VERDICT.revision_hash.value,
        ),
        (
            "decide",
            "operation",
            RevisionKind.DETERMINISTIC_OPERATION,
            DETERMINISTIC.revision_hash.value,
        ),
        (
            "decide",
            "outputs.schema",
            RevisionKind.SCHEMA,
            SCHEMA_VERDICT.revision_hash.value,
        ),
        (
            "confirm",
            "outputs.schema",
            RevisionKind.SCHEMA,
            SCHEMA_APPROVAL.revision_hash.value,
        ),
        (
            "hand_off",
            "operation",
            RevisionKind.ADAPTER_OPERATION,
            ADAPTER.revision_hash.value,
        ),
        (
            "hand_off",
            "outputs.schema",
            RevisionKind.SCHEMA,
            SCHEMA_RECEIPT.revision_hash.value,
        ),
    }
    assert composed.graph.unresolved_references == ()


def test_an_unresolved_reference_is_named_instead_of_stopping_the_preview() -> None:
    composed = preview(answers=without_publication(PROFILE, ADAPTER))

    assert [
        (entry.reason, entry.site.node, entry.site.field, entry.reference.ref)
        for entry in composed.graph.unresolved_references
    ] == [
        (
            ReferenceRefusalReason.UNPUBLISHED_REVISION,
            "implement",
            "profile",
            "builder_method",
        ),
        (
            ReferenceRefusalReason.UNPUBLISHED_REVISION,
            "hand_off",
            "operation",
            "requirement_comment",
        ),
    ]
    assert [node.id for node in composed.graph.nodes] == [
        "implement",
        "panel",
        "decide",
        "confirm",
        "hand_off",
    ]


def test_a_withdrawn_skill_is_named_unresolved_instead_of_ending_the_preview() -> None:
    composed = preview(answers=without_publication(SKILL, PROFILE))

    assert [
        (entry.reason, entry.site.node, entry.site.field, entry.reference.ref)
        for entry in composed.graph.unresolved_references
    ] == [
        (
            ReferenceRefusalReason.UNPUBLISHED_REVISION,
            "implement",
            "profile",
            "builder_method",
        ),
        (
            ReferenceRefusalReason.UNPUBLISHED_REVISION,
            "implement",
            "skills",
            "workspace_discipline",
        ),
    ]
    implement = node_of(composed.graph, "implement")
    assert RuntimeCapability.SKILL_INSTALLATION in demands_of(implement)
    assert [
        requirement.subject
        for requirement in implement.demands
        if requirement.capability is RuntimeCapability.TOOL_GRANTS
    ] == [CapabilitySubject.of(RevisionKind.TOOL, TOOL_REFERENCE)]


def test_a_skill_nobody_read_keeps_its_grants_unknown_instead_of_ending_the_preview() -> (
    None
):
    composed = preview(skills=PublishedSkills())

    assert [
        (entry.site.node, entry.site.field, entry.reference)
        for entry in composed.graph.unknown_skill_grants
    ] == [("implement", "skills", SKILL_REFERENCE)]
    assert "unknown rather than none" in str(composed.graph.unknown_skill_grants[0])
    implement = node_of(composed.graph, "implement")
    assert RuntimeCapability.SKILL_INSTALLATION in demands_of(implement)
    assert [
        requirement.subject
        for requirement in implement.demands
        if requirement.capability is RuntimeCapability.TOOL_GRANTS
    ] == [CapabilitySubject.of(RevisionKind.TOOL, TOOL_REFERENCE)]


def test_an_unread_skill_and_an_unresolved_reference_are_named_in_one_preview() -> None:
    composed = preview(answers=without_publication(PROFILE), skills=PublishedSkills())

    assert [
        (entry.site.node, entry.site.field, entry.reference.ref)
        for entry in composed.graph.unresolved_references
    ] == [("implement", "profile", "builder_method")]
    assert [
        (entry.site.node, entry.reference.ref)
        for entry in composed.graph.unknown_skill_grants
    ] == [("implement", "workspace_discipline")]
    assert [node.id for node in composed.graph.nodes] == [
        "implement",
        "panel",
        "decide",
        "confirm",
        "hand_off",
    ]


def test_a_withdrawn_skill_is_no_unknown_reading_because_it_installs_nothing() -> None:
    composed = preview(answers=without_publication(SKILL), skills=PublishedSkills())

    assert composed.graph.unknown_skill_grants == ()


def test_a_reference_that_pins_no_revision_hash_is_named_as_malformed() -> None:
    composed = compose_preview(
        WorkflowRevision(UNPINNED_PARENT).revision_hash,
        parsed(UNPINNED_PARENT),
        SubworkflowBinding(),
        ROLE_MATRIX,
        SKILL_CONTENTS,
        FULL_ATTESTATION,
        PublishedRegistry(FULL_REGISTRY),
        ConfigurationBinding.PROPOSED,
    )

    assert [
        (entry.reason, entry.site.field)
        for entry in composed.graph.unresolved_references
    ] == [
        (ReferenceRefusalReason.MALFORMED_REVISION, "outputs.schema"),
        (ReferenceRefusalReason.MALFORMED_REVISION, "profile"),
    ]


def test_a_fully_attested_preview_carries_the_executable_verdict() -> None:
    composed = preview()

    assert composed.executability == Executable()
    assert all(node.unproven == () for node in composed.graph.nodes)


def test_a_node_names_the_capability_it_is_still_waiting_for() -> None:
    composed = preview(
        attested=without(FULL_ATTESTATION, RuntimeCapability.DETERMINISTIC_OPERATIONS)
    )

    assert isinstance(composed.executability, NotExecutable)
    assert [
        (node.id, tuple(refusal.requirement.capability for refusal in node.unproven))
        for node in composed.graph.nodes
        if node.unproven
    ] == [
        ("decide", (RuntimeCapability.DETERMINISTIC_OPERATIONS,)),
    ]


def test_an_unproven_demand_of_the_run_is_named_on_the_node_that_raised_it() -> None:
    composed = preview(
        attested=without(FULL_ATTESTATION, RuntimeCapability.TOOL_GRANTS)
    )

    assert isinstance(composed.executability, NotExecutable)
    assert sorted(
        map(
            str,
            (refusal for node in composed.graph.nodes for refusal in node.unproven),
        )
    ) == sorted(map(str, composed.executability.refusals))


@pytest.mark.parametrize(
    "configuration", (ConfigurationBinding.PROPOSED, ConfigurationBinding.BOUND)
)
def test_a_preview_says_whether_its_configuration_is_proposed_or_bound(
    configuration: ConfigurationBinding,
) -> None:
    composed = preview(configuration=configuration)

    assert composed.configuration is configuration
    assert composed.workflow_revision_hash == WorkflowRevision(PARENT).revision_hash


def test_one_role_matrix_reached_in_any_order_is_one_preview() -> None:
    assert preview(role_matrix=ROLE_MATRIX) == preview(
        role_matrix=tuple(reversed(ROLE_MATRIX))
    )


@pytest.mark.proves("the-preview-names-every-declared-graph-input-with-its-schema")
def test_a_root_preview_names_its_declared_graph_input_and_schema() -> None:
    composed = preview(document=CHILD, attested=attesting(demanded(CHILD)))

    assert [
        (entry.name, entry.schema_reference.ref, entry.schema_reference.revision)
        for entry in composed.graph.graph_inputs
    ] == [("candidate", "workspace_candidate", SCHEMA_CANDIDATE.revision_hash.value)]


_PANEL_TAIL = b"\n  - id: decide\n"
ITERATING_PARENT = PARENT.replace(
    _PANEL_TAIL,
    (
        "\n    iterate:"
        "\n      maximum_rounds: 4"
        "\n      until:"
        "\n        output: verdict"
        f"\n        schema: {{ref: review_verdict, revision: "
        f"{SCHEMA_VERDICT.revision_hash.value}}}"
    ).encode()
    + _PANEL_TAIL,
    1,
)


@pytest.mark.proves("the-preview-names-every-iteration-with-its-bound-and-its-green")
def test_the_preview_names_each_iteration_with_its_bound_and_its_green() -> None:
    """An operator reading the preview must see that a node repeats, how often at
    most, and what would end it — otherwise a bounded loop and a single run draw
    identically until one of them has already run four times."""
    composed = preview(document=ITERATING_PARENT)

    iteration = node_of(composed.graph, "panel").iteration

    assert iteration is not None
    assert iteration.maximum_rounds == 4
    assert iteration.green_output == "verdict"
    assert iteration.green_schema.revision == SCHEMA_VERDICT.revision_hash.value


@pytest.mark.proves("the-preview-names-every-iteration-with-its-bound-and-its-green")
def test_a_node_that_does_not_repeat_draws_no_iteration() -> None:
    assert node_of(preview().graph, "panel").iteration is None
