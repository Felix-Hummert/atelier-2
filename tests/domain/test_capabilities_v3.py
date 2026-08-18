from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import pytest

from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.application.bind_subworkflow_boundaries import (
    bind_subworkflow_boundaries,
)
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
    AgentConfigurationSubject,
    AttestedCapabilities,
    CapabilityAttestation,
    CapabilityRequirement,
    CapabilitySubject,
    Executable,
    NotExecutable,
    OutputProfile,
    OutputProfileSubject,
    PublishedSkills,
    RegistryRevisionSubject,
    RoleBindingRefusalReason,
    RoleBindingRefused,
    RuntimeCapability,
    SkillContents,
    SubjectIdentity,
    decide_executability,
    required_capabilities,
)
from atelier2.contracts.revisions_v3 import RevisionKind
from atelier2.contracts.run_configuration_v3 import ReferenceSite
from atelier2.contracts.runs import WorkflowRevision
from atelier2.contracts.workflow_bindings_v3 import SubworkflowBinding
from atelier2.contracts.workflows_v3 import (
    AgentNodeV3,
    VersionedReference,
    WorkflowGraphV3,
)
from atelier2.ports.workflow_revisions import (
    PublishedWorkflowFound,
    PublishedWorkflowMissing,
    ResolvePublishedWorkflowResult,
)

PARENT_TEMPLATE = b"""format_version: 3
name: Build a candidate, review it in a panel and hand off the verdict
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Build every acceptance sentence of the bound story.
    profile: {ref: builder_method, revision: profile-1}
    skills:
      - {ref: workspace_discipline, revision: skill-1}
    tools:
      - {ref: repository_write, revision: tool-1}
    required_context:
      - name: story
        source: {ref: requirement, revision: source-1, selector: story_acceptance}
    available_context:
      - name: decisions
        source: {ref: decision_record_index, revision: source-2}
        read_operations:
          - {ref: search, revision: read-1}
    outputs:
      - name: candidate
        schema: {ref: workspace_candidate, revision: schema-1}
  - id: panel
    type: subworkflow
    depends_on: [implement]
    workflow: {ref: review_panel, revision: <child>}
    inputs:
      - name: candidate
        from: {node: implement, output: candidate}
    outputs:
      - name: verdict
        schema: {ref: review_verdict, revision: schema-2}
  - id: decide
    type: deterministic
    depends_on: [panel]
    operation: {ref: settle_the_panel, revision: deterministic-1}
    inputs:
      - name: verdict
        from: {node: panel, output: verdict}
    outputs:
      - name: verdict
        schema: {ref: review_verdict, revision: schema-2}
  - id: hand_off
    type: action
    depends_on: [panel]
    operation: {ref: requirement_comment, revision: adapter-1}
    inputs:
      - name: verdict
        from: {node: panel, output: verdict}
    outputs:
      - name: receipt
        schema: {ref: comment_receipt, revision: schema-3}
"""

CHILD = b"""format_version: 3
name: Merge the panel's review verdicts into one
graph_inputs:
  - name: candidate
    schema: {ref: workspace_candidate, revision: schema-1}
graph_outputs:
  - name: verdict
    from: {node: merge, output: merged}
nodes:
  - id: read_it
    type: agent
    role: reviewer
    mode: headless
    instruction: Judge the candidate against the acceptance sentences.
    inputs:
      - name: candidate
        from: {graph_input: candidate}
    outputs:
      - name: opinion
        schema: {ref: review_opinion, revision: schema-4}
  - id: run_it
    type: agent
    role: reviewer
    mode: headless
    instruction: Run the gates the candidate claims are green.
    outputs:
      - name: opinion
        schema: {ref: review_opinion, revision: schema-4}
  - id: merge
    type: deterministic
    depends_on: [read_it, run_it]
    join: all_succeeded
    operation: {ref: merge_review_verdicts, revision: deterministic-2}
    inputs:
      - name: read
        from: {node: read_it, output: opinion}
      - name: run
        from: {node: run_it, output: opinion}
    outputs:
      - name: merged
        schema: {ref: review_verdict, revision: schema-2}
"""

UNRESTRICTED_AGENT = """format_version: 3
name: Build with whatever the bound definition grants
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Build the story with whatever the bound definition already grants.
    skills:
      - {{ref: {skill}, revision: skill-1}}
    outputs:
      - name: candidate
        schema: {{ref: workspace_candidate, revision: schema-1}}
"""

CHILD_REVISION = WorkflowRevision(CHILD).revision_hash.value
CHILD_REFERENCE = VersionedReference(ref="review_panel", revision=CHILD_REVISION)
CHILD_CHAIN = (CHILD_REFERENCE,)
PARENT = PARENT_TEMPLATE.replace(b"<child>", CHILD_REVISION.encode("ascii"))
SKILL_REFERENCE = VersionedReference(ref="workspace_discipline", revision="skill-1")
CARRIED_GRANT = VersionedReference(ref="workspace_write", revision="tool-2")
SKILL_CONTENTS = PublishedSkills((SkillContents("skill-1", (CARRIED_GRANT,)),))


def _auth() -> AuthProfileRevision:
    return AuthProfileRevision(
        "workshop", 1, ProviderId("anthropic"), AuthMode.SUBSCRIPTION
    )


def _configuration(model: str) -> AgentConfigurationRevision:
    return _binding("builder", model).configuration


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


ROLE_MATRIX = (
    _binding("builder", "claude-opus-5"),
    _binding("reviewer", "claude-sonnet-5"),
)
BUILDER_SUBJECT = CapabilitySubject(
    AgentConfigurationSubject(_configuration("claude-opus-5").revision_hash.value),
    "builder",
)
REVIEWER_SUBJECT = CapabilitySubject(
    AgentConfigurationSubject(_configuration("claude-sonnet-5").revision_hash.value),
    "reviewer",
)
SINGLE_OUTPUT = CapabilitySubject(
    OutputProfileSubject(OutputProfile.SINGLE_JSON_OUTPUT)
)


def registry_subject(kind: RevisionKind, name: str, revision: str) -> CapabilitySubject:
    return CapabilitySubject(RegistryRevisionSubject(kind, revision), name)


@dataclass(frozen=True)
class PublishedWorkflows:
    documents: Mapping[tuple[str, str], bytes]

    def resolve(self, reference: VersionedReference) -> ResolvePublishedWorkflowResult:
        document = self.documents.get((reference.ref, reference.revision))
        if document is None:
            return PublishedWorkflowMissing()
        return PublishedWorkflowFound(WorkflowRevision(document))


def parsed(document: bytes) -> WorkflowGraphV3:
    graph = parse_workflow_document(document)
    assert isinstance(graph, WorkflowGraphV3)
    return graph


MAXIMUM_ITERATION_ROUNDS = 8


def bound_children(document: bytes) -> SubworkflowBinding:
    return bind_subworkflow_boundaries(
        parsed(document),
        PublishedWorkflows({("review_panel", CHILD_REVISION): CHILD}),
        parse_workflow_document,
        2,
        MAXIMUM_ITERATION_ROUNDS,
    )


def required(
    document: bytes = PARENT,
    role_matrix: Sequence[ResolvedAgentBinding] = ROLE_MATRIX,
    skills: PublishedSkills = SKILL_CONTENTS,
) -> tuple[CapabilityRequirement, ...]:
    return required_capabilities(
        parsed(document), bound_children(document), tuple(role_matrix), skills
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


def without(
    attested: AttestedCapabilities, capability: RuntimeCapability
) -> AttestedCapabilities:
    return AttestedCapabilities(
        tuple(entry for entry in attested.entries if entry.capability is not capability)
    )


def without_subject(
    attested: AttestedCapabilities,
    capability: RuntimeCapability,
    identity: SubjectIdentity,
) -> AttestedCapabilities:
    return AttestedCapabilities(
        tuple(
            CapabilityAttestation(entry.capability, entry.subjects - {identity})
            if entry.capability is capability
            else entry
            for entry in attested.entries
        )
    )


def refused(decision: Executable | NotExecutable) -> tuple[str, ...]:
    assert isinstance(decision, NotExecutable)
    return tuple(str(refusal) for refusal in decision.refusals)


def site(
    field: str,
    node: str,
    entry: str | None = None,
    chain: tuple[VersionedReference, ...] = (),
) -> ReferenceSite:
    return ReferenceSite(field, node, entry, chain)


EXPECTED_PARENT_REQUIREMENTS = (
    CapabilityRequirement(
        RuntimeCapability.CONTEXT_MATERIALIZATION,
        site("required_context.source", "implement", "story"),
        registry_subject(RevisionKind.CONTEXT_SOURCE, "requirement", "source-1"),
    ),
    CapabilityRequirement(
        RuntimeCapability.AGENT_EXECUTION, site("role", "implement"), BUILDER_SUBJECT
    ),
    CapabilityRequirement(
        RuntimeCapability.OUTPUT_VALIDATION, site("outputs", "implement"), SINGLE_OUTPUT
    ),
    CapabilityRequirement(
        RuntimeCapability.CONTEXT_RESOLUTION,
        site("available_context.source", "implement", "decisions"),
        registry_subject(
            RevisionKind.CONTEXT_SOURCE, "decision_record_index", "source-2"
        ),
    ),
    CapabilityRequirement(
        RuntimeCapability.CONTEXT_RESOLUTION,
        site("available_context.read_operations", "implement", "decisions"),
        registry_subject(RevisionKind.READ_OPERATION, "search", "read-1"),
    ),
    CapabilityRequirement(
        RuntimeCapability.SKILL_INSTALLATION,
        site("skills", "implement"),
        registry_subject(RevisionKind.SKILL, "workspace_discipline", "skill-1"),
    ),
    CapabilityRequirement(
        RuntimeCapability.TOOL_GRANTS,
        site("skills", "implement", chain=(SKILL_REFERENCE,)),
        registry_subject(RevisionKind.TOOL, "workspace_write", "tool-2"),
    ),
    CapabilityRequirement(
        RuntimeCapability.TOOL_GRANTS,
        site("tools", "implement"),
        registry_subject(RevisionKind.TOOL, "repository_write", "tool-1"),
    ),
    CapabilityRequirement(
        RuntimeCapability.DAG_SCHEDULING, site("depends_on", "panel")
    ),
    CapabilityRequirement(
        RuntimeCapability.SUBWORKFLOW_EXECUTION, site("workflow", "panel")
    ),
    CapabilityRequirement(
        RuntimeCapability.AGENT_EXECUTION,
        site("role", "read_it", chain=CHILD_CHAIN),
        REVIEWER_SUBJECT,
    ),
    CapabilityRequirement(
        RuntimeCapability.OUTPUT_VALIDATION,
        site("outputs", "read_it", chain=CHILD_CHAIN),
        SINGLE_OUTPUT,
    ),
    CapabilityRequirement(
        RuntimeCapability.AGENT_EXECUTION,
        site("role", "run_it", chain=CHILD_CHAIN),
        REVIEWER_SUBJECT,
    ),
    CapabilityRequirement(
        RuntimeCapability.OUTPUT_VALIDATION,
        site("outputs", "run_it", chain=CHILD_CHAIN),
        SINGLE_OUTPUT,
    ),
    CapabilityRequirement(
        RuntimeCapability.DAG_SCHEDULING, site("depends_on", "merge", chain=CHILD_CHAIN)
    ),
    CapabilityRequirement(
        RuntimeCapability.DETERMINISTIC_OPERATIONS,
        site("operation", "merge", chain=CHILD_CHAIN),
        registry_subject(
            RevisionKind.DETERMINISTIC_OPERATION,
            "merge_review_verdicts",
            "deterministic-2",
        ),
    ),
    CapabilityRequirement(
        RuntimeCapability.DETERMINISTIC_OPERATIONS,
        site("operation", "decide"),
        registry_subject(
            RevisionKind.DETERMINISTIC_OPERATION, "settle_the_panel", "deterministic-1"
        ),
    ),
    CapabilityRequirement(
        RuntimeCapability.EXTERNAL_EFFECTS,
        site("operation", "hand_off"),
        registry_subject(
            RevisionKind.ADAPTER_OPERATION, "requirement_comment", "adapter-1"
        ),
    ),
)


def test_the_capability_vocabulary_is_the_closed_set_the_record_names() -> None:
    assert tuple(capability.value for capability in RuntimeCapability) == (
        "dag_scheduling",
        "agent_execution",
        "output_validation",
        "skill_installation",
        "tool_grants",
        "context_materialization",
        "context_resolution",
        "isolated_workspace",
        "external_effects",
        "deterministic_operations",
        "subworkflow_execution",
    )


def test_every_node_form_demands_exactly_the_capabilities_the_record_names() -> None:
    assert required() == EXPECTED_PARENT_REQUIREMENTS


def test_a_definition_without_restrictions_demands_only_what_its_node_performs() -> (
    None
):
    document = b"""format_version: 3
name: Build the story under an unrestricted definition
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Build the story, with whatever the bound definition grants.
    outputs:
      - name: candidate
        schema: {ref: workspace_candidate, revision: schema-1}
"""
    assert required(document) == (
        CapabilityRequirement(
            RuntimeCapability.AGENT_EXECUTION,
            site("role", "implement"),
            BUILDER_SUBJECT,
        ),
        CapabilityRequirement(
            RuntimeCapability.OUTPUT_VALIDATION,
            site("outputs", "implement"),
            SINGLE_OUTPUT,
        ),
    )


def test_a_wait_node_demands_only_the_context_it_requires() -> None:
    document = b"""format_version: 3
name: Ask the operator whether the candidate is accepted
nodes:
  - id: confirm
    type: wait
    prompt: Does the operator accept this candidate?
    required_context:
      - name: story
        source: {ref: requirement, revision: source-1, selector: story_acceptance}
    outputs:
      - name: answer
        schema: {ref: operator_answer, revision: schema-5}
"""
    assert required(document) == (
        CapabilityRequirement(
            RuntimeCapability.CONTEXT_MATERIALIZATION,
            site("required_context.source", "confirm", "story"),
            registry_subject(RevisionKind.CONTEXT_SOURCE, "requirement", "source-1"),
        ),
    )


def test_a_valid_document_is_executable_once_its_capabilities_are_attested() -> None:
    requirements = required()

    assert decide_executability(requirements, attesting(requirements)) == Executable()


def test_the_same_document_is_refused_naming_the_node_and_the_missing_capability() -> (
    None
):
    requirements = required()
    attested = without(attesting(requirements), RuntimeCapability.EXTERNAL_EFFECTS)

    assert refused(decide_executability(requirements, attested)) == (
        (
            "node 'hand_off' field 'operation' demands external_effects for "
            "requirement_comment@adapter_operation:adapter-1: the bound capability "
            "revision attests no "
            "external_effects [unattested_capability]"
        ),
    )


def test_an_attested_capability_proves_only_the_revisions_it_enumerates() -> None:
    requirements = required()
    attested = without_subject(
        attesting(requirements),
        RuntimeCapability.DETERMINISTIC_OPERATIONS,
        RegistryRevisionSubject(
            RevisionKind.DETERMINISTIC_OPERATION, "deterministic-2"
        ),
    )

    assert refused(decide_executability(requirements, attested)) == (
        (
            f"review_panel@{CHILD_REVISION} > node 'merge' field 'operation' demands "
            "deterministic_operations for merge_review_verdicts@"
            "deterministic_operation:deterministic-2: deterministic_operations "
            "enumerates no such revision [unattested_subject]"
        ),
    )


def test_a_refusal_names_every_unproven_requirement_not_only_the_first() -> None:
    requirements = required()
    attested = without(
        without(attesting(requirements), RuntimeCapability.AGENT_EXECUTION),
        RuntimeCapability.DAG_SCHEDULING,
    )

    decision = decide_executability(requirements, attested)

    assert isinstance(decision, NotExecutable)
    assert tuple(
        (refusal.requirement.capability, refusal.requirement.site.node)
        for refusal in decision.refusals
    ) == (
        (RuntimeCapability.AGENT_EXECUTION, "implement"),
        (RuntimeCapability.DAG_SCHEDULING, "panel"),
        (RuntimeCapability.AGENT_EXECUTION, "read_it"),
        (RuntimeCapability.AGENT_EXECUTION, "run_it"),
        (RuntimeCapability.DAG_SCHEDULING, "merge"),
    )


def test_an_attested_skill_carrying_an_unattested_grant_refuses_the_whole_run() -> None:
    requirements = required()
    attested = without_subject(
        attesting(requirements),
        RuntimeCapability.TOOL_GRANTS,
        RegistryRevisionSubject(RevisionKind.TOOL, "tool-2"),
    )

    assert refused(decide_executability(requirements, attested)) == (
        (
            "workspace_discipline@skill-1 > node 'implement' field 'skills' demands "
            "tool_grants for workspace_write@tool:tool-2: tool_grants enumerates no "
            "such revision [unattested_subject]"
        ),
    )


def test_the_same_document_starts_once_the_carried_grant_is_attested() -> None:
    requirements = required()

    implement = parsed(PARENT).node("implement")

    assert decide_executability(requirements, attesting(requirements)) == Executable()
    assert isinstance(implement, AgentNodeV3)
    assert CARRIED_GRANT not in implement.tools


def test_required_context_alone_never_demands_context_resolution() -> None:
    document = b"""format_version: 3
name: Build the story from context the run materialized
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Build the story from the material the run materialized.
    required_context:
      - name: story
        source: {ref: requirement, revision: source-1, selector: story_acceptance}
    outputs:
      - name: candidate
        schema: {ref: workspace_candidate, revision: schema-1}
"""
    materialization_only = AttestedCapabilities(
        (
            CapabilityAttestation(
                RuntimeCapability.CONTEXT_MATERIALIZATION,
                frozenset(
                    {RegistryRevisionSubject(RevisionKind.CONTEXT_SOURCE, "source-1")}
                ),
            ),
            CapabilityAttestation(
                RuntimeCapability.AGENT_EXECUTION,
                frozenset({BUILDER_SUBJECT.identity}),
            ),
            CapabilityAttestation(
                RuntimeCapability.OUTPUT_VALIDATION,
                frozenset({SINGLE_OUTPUT.identity}),
            ),
        )
    )

    assert (
        decide_executability(required(document), materialization_only) == Executable()
    )


def test_a_context_source_attestation_never_proves_a_read_operation_of_one_hash() -> (
    None
):
    document = b"""format_version: 3
name: Read the decision record index the way the run allows
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Read the record index the way the run allows.
    available_context:
      - name: decisions
        source: {ref: decision_record_index, revision: one-revision}
        read_operations:
          - {ref: search, revision: one-revision}
    outputs:
      - name: candidate
        schema: {ref: workspace_candidate, revision: schema-1}
"""
    requirements = required(document)
    context_source_only = AttestedCapabilities(
        (
            CapabilityAttestation(
                RuntimeCapability.CONTEXT_RESOLUTION,
                frozenset(
                    {
                        RegistryRevisionSubject(
                            RevisionKind.CONTEXT_SOURCE, "one-revision"
                        )
                    }
                ),
            ),
            CapabilityAttestation(
                RuntimeCapability.AGENT_EXECUTION,
                frozenset({BUILDER_SUBJECT.identity}),
            ),
            CapabilityAttestation(
                RuntimeCapability.OUTPUT_VALIDATION,
                frozenset({SINGLE_OUTPUT.identity}),
            ),
        )
    )

    assert refused(decide_executability(requirements, context_source_only)) == (
        (
            "node 'implement' field 'available_context.read_operations' 'decisions' "
            "demands context_resolution for search@read_operation:one-revision: "
            "context_resolution enumerates no such revision [unattested_subject]"
        ),
    )


def test_an_agent_node_demands_the_output_profile_its_declared_arity_asks_for() -> None:
    def profiles(document: bytes) -> tuple[CapabilitySubject | None, ...]:
        return tuple(
            requirement.subject
            for requirement in required(document)
            if requirement.capability is RuntimeCapability.OUTPUT_VALIDATION
        )

    silent = b"""format_version: 3
name: Leave the workspace behind and declare no output
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Leave the workspace behind and declare nothing.
"""
    plural = b"""format_version: 3
name: Return the candidate and the note beside it
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Return the candidate and the note beside it.
    outputs:
      - name: candidate
        schema: {ref: workspace_candidate, revision: schema-1}
      - name: note
        schema: {ref: review_opinion, revision: schema-4}
"""

    assert profiles(silent) == (
        CapabilitySubject(OutputProfileSubject(OutputProfile.NO_OUTPUT)),
    )
    assert profiles(plural) == (
        CapabilitySubject(OutputProfileSubject(OutputProfile.MULTIPLE_JSON_OUTPUTS)),
    )


def test_a_node_requiring_an_operator_is_refused_against_a_headless_binding() -> None:
    document = b"""format_version: 3
name: Decide the candidate with the operator watching
nodes:
  - id: decide_together
    type: agent
    role: builder
    mode: interactive
    instruction: Decide this with the operator watching.
    outputs:
      - name: candidate
        schema: {ref: workspace_candidate, revision: schema-1}
        confirmed_by: operator
"""

    with pytest.raises(RoleBindingRefused) as refusal:
        required(document)

    assert (
        refusal.value.refusal.reason
        is RoleBindingRefusalReason.INCOMPATIBLE_EXECUTION_MODE
    )
    assert "interactive" in str(refusal.value) and "headless" in str(refusal.value)


TOOL_USING_NODE = b"""format_version: 3
name: Work the candidate in the workspace
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless_with_tools
    instruction: Change the workspace and say what you changed.
    outputs:
      - name: candidate
        schema: {ref: workspace_candidate, revision: schema-1}
"""


def test_a_node_requiring_tools_is_refused_against_a_tool_free_binding() -> None:
    """A tool-free configuration cannot answer a node that declares tool work.

    The refusal is the same one that guards the interactive mode, and it has to
    be: running this node under a call that can touch nothing would deliver an
    answer about a workspace nobody changed.
    """

    with pytest.raises(RoleBindingRefused) as refusal:
        required(TOOL_USING_NODE)

    assert (
        refusal.value.refusal.reason
        is RoleBindingRefusalReason.INCOMPATIBLE_EXECUTION_MODE
    )
    assert AgentExecutionCapability.HEADLESS_WITH_TOOLS.value in str(refusal.value)


def test_the_same_node_binds_against_a_configuration_declaring_the_tool_capability() -> (
    None
):
    tool_using = _binding(
        "builder", "claude-opus-5", AgentExecutionCapability.HEADLESS_WITH_TOOLS
    )

    requirements = required(TOOL_USING_NODE, role_matrix=(tool_using,))

    assert [requirement.capability for requirement in requirements] == [
        RuntimeCapability.AGENT_EXECUTION,
        RuntimeCapability.OUTPUT_VALIDATION,
    ]
    assert requirements[0].subject == CapabilitySubject(
        AgentConfigurationSubject(tool_using.configuration.revision_hash.value),
        "builder",
    )


def test_the_same_node_binds_against_a_configuration_declaring_that_mode() -> None:
    document = b"""format_version: 3
name: Decide the candidate with the operator watching
nodes:
  - id: decide_together
    type: agent
    role: builder
    mode: interactive
    instruction: Decide this with the operator watching.
    outputs:
      - name: candidate
        schema: {ref: workspace_candidate, revision: schema-1}
        confirmed_by: operator
"""
    interactive = _binding(
        "builder", "claude-opus-5", AgentExecutionCapability.INTERACTIVE
    )

    requirements = required(document, role_matrix=(interactive,))

    assert [requirement.capability for requirement in requirements] == [
        RuntimeCapability.AGENT_EXECUTION,
        RuntimeCapability.OUTPUT_VALIDATION,
    ]
    assert requirements[0].subject == CapabilitySubject(
        AgentConfigurationSubject(interactive.configuration.revision_hash.value),
        "builder",
    )


def test_a_manifest_proves_a_revision_however_the_author_named_it() -> None:
    pinned = UNRESTRICTED_AGENT.format(skill="workspace_discipline").encode("utf-8")
    renamed = UNRESTRICTED_AGENT.format(skill="house_discipline").encode("utf-8")

    decision = decide_executability(required(renamed), attesting(required(pinned)))

    assert decision == Executable()


def test_an_unbound_role_refuses_the_document_naming_the_node_and_the_role() -> None:
    with pytest.raises(RoleBindingRefused) as refusal:
        required(role_matrix=(ROLE_MATRIX[0],))

    assert refusal.value.refusal.reason is RoleBindingRefusalReason.UNBOUND_ROLE
    assert str(refusal.value) == (
        f"review_panel@{CHILD_REVISION} > node 'read_it' field 'role': no bound "
        "agent-configuration revision carries role 'reviewer' [unbound_role]"
    )


def test_a_skill_whose_carried_grants_were_never_read_is_loud() -> None:
    with pytest.raises(ValueError, match="skill-1"):
        required(skills=PublishedSkills())


def test_a_capability_revision_attests_each_capability_at_most_once() -> None:
    with pytest.raises(ValueError, match="once"):
        AttestedCapabilities(
            (
                CapabilityAttestation(
                    RuntimeCapability.TOOL_GRANTS,
                    frozenset({RegistryRevisionSubject(RevisionKind.TOOL, "a")}),
                ),
                CapabilityAttestation(
                    RuntimeCapability.TOOL_GRANTS,
                    frozenset({RegistryRevisionSubject(RevisionKind.TOOL, "b")}),
                ),
            )
        )


def test_a_refused_document_names_at_least_one_unproven_requirement() -> None:
    with pytest.raises(ValueError, match="unproven"):
        NotExecutable(())
