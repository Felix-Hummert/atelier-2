"""What a bound V3 document demands of a runtime, and whether that runtime proved it.

Decision 0006 stages execution rather than the format: a valid V3 document stays
valid forever, and only the third validation phase — executability — depends on what
the deployment can actually perform. This module owns that phase's pure half. It
derives, over the closure of everything a document references, the capabilities each
node demands, and it decides whether one attested capability revision proves them.

Nothing here is authored. A capability revision is produced by a build that proved
its entries, so this contract only reads a manifest; the published, build-produced
revision carrying build identity and evidence, and the run start that binds it,
belong to the run-start path rather than to this decision.

Requirements are located by the reference geography the run configuration already
owns, `ReferenceSite`, extended to the two shape fields that demand a capability
without pinning a reference: `depends_on`, whose extra edges demand scheduling, and
`role`, whose binding demands agent execution.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum

from atelier2.contracts.agents import AgentExecutionCapability, ResolvedAgentBinding
from atelier2.contracts.revisions_v3 import RevisionKind
from atelier2.contracts.run_configuration_v3 import ReferenceChain, ReferenceSite
from atelier2.contracts.workflow_bindings_v3 import BoundSubworkflow, SubworkflowBinding
from atelier2.contracts.workflows_v3 import (
    ActionNodeV3,
    AgentNodeV3,
    DeterministicNodeV3,
    SubworkflowNodeV3,
    VersionedReference,
    WorkflowGraphV3,
    WorkflowNodeV3,
)


class RuntimeCapability(StrEnum):
    """The closed set of capabilities a runtime attests, per decision 0006.

    A refusal must name something stable, so these tokens are the contract itself.
    `isolated_workspace` is attestable but demanded by no node yet: it enters through
    an output schema materialized from an isolated workspace, and nothing a published
    schema revision carries today says that a schema is one.
    """

    DAG_SCHEDULING = "dag_scheduling"
    AGENT_EXECUTION = "agent_execution"
    OUTPUT_VALIDATION = "output_validation"
    SKILL_INSTALLATION = "skill_installation"
    TOOL_GRANTS = "tool_grants"
    CONTEXT_MATERIALIZATION = "context_materialization"
    CONTEXT_RESOLUTION = "context_resolution"
    ISOLATED_WORKSPACE = "isolated_workspace"
    EXTERNAL_EFFECTS = "external_effects"
    DETERMINISTIC_OPERATIONS = "deterministic_operations"
    SUBWORKFLOW_EXECUTION = "subworkflow_execution"


class OutputProfile(StrEnum):
    """The output shape the provider-neutral core enforces for one agent node.

    The document alone declares `outputs[*].schema`; a profile says which shape the
    core can enforce at all, so the arity a node authored names its own profile
    instead of a builder default. Only `single-json-output/v1` (#57) exists today,
    which is why the other two shapes name a form no build attests yet.
    """

    NO_OUTPUT = "no-output/v1"
    SINGLE_JSON_OUTPUT = "single-json-output/v1"
    MULTIPLE_JSON_OUTPUTS = "multiple-json-outputs/v1"


@dataclass(frozen=True, slots=True)
class RegistryRevisionSubject:
    """One published revision, in the registry the reference is read under.

    The kind travels with the hash because identical bytes published under two
    kinds are two different things: without it one `context_source` attestation
    would prove a `read_operation` of the same content.
    """

    kind: RevisionKind
    revision_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, RevisionKind):
            raise TypeError("a registry subject names its registry through the kind")
        if not self.revision_hash:
            raise ValueError("a registry subject names the revision it demands")

    def __str__(self) -> str:
        return f"{self.kind.value}:{self.revision_hash}"


@dataclass(frozen=True, slots=True)
class AgentConfigurationSubject:
    """The agent-configuration revision one role binds to, which is no registry."""

    revision_hash: str

    def __post_init__(self) -> None:
        if not self.revision_hash:
            raise ValueError("an agent configuration subject names its revision")

    def __str__(self) -> str:
        return f"agent_configuration:{self.revision_hash}"


@dataclass(frozen=True, slots=True)
class OutputProfileSubject:
    """The output shape a build proves it can enforce."""

    profile: OutputProfile

    def __post_init__(self) -> None:
        if not isinstance(self.profile, OutputProfile):
            raise TypeError("an output profile subject uses the closed vocabulary")

    def __str__(self) -> str:
        return self.profile.value


type SubjectIdentity = (
    RegistryRevisionSubject | AgentConfigurationSubject | OutputProfileSubject
)


@dataclass(frozen=True, slots=True)
class CapabilitySubject:
    """The exact identity one capability entry must enumerate, and its authored name.

    A manifest is matched on `identity` alone. `name` is the author's word for the
    thing and travels only so a refusal reads like the document; matching it would
    let a rename widen what a build proved, and this is the trust boundary that
    decides what a run may spend and touch. A subject nothing in the document names
    — an output profile — carries no name.
    """

    identity: SubjectIdentity
    name: str | None = None

    def __post_init__(self) -> None:
        if self.name == "":
            raise ValueError("a capability subject named at all is named nonempty")

    @classmethod
    def of(cls, kind: RevisionKind, reference: VersionedReference) -> CapabilitySubject:
        return cls(RegistryRevisionSubject(kind, reference.revision), reference.ref)

    def __str__(self) -> str:
        named = "" if self.name is None else f"{self.name}@"
        return f"{named}{self.identity}"


@dataclass(frozen=True, slots=True)
class CapabilityRequirement:
    """One capability a bound document demands, and the exact place it entered."""

    capability: RuntimeCapability
    site: ReferenceSite
    subject: CapabilitySubject | None = None

    def __str__(self) -> str:
        subject = "" if self.subject is None else f" for {self.subject}"
        return f"{self.site} demands {self.capability.value}{subject}"


@dataclass(frozen=True)
class CapabilityAttestation:
    """One manifest entry: a capability, and the exact revisions a build proved it for.

    An entry is a manifest, not a Boolean. A requirement naming a revision is proven
    only when this entry enumerates that revision, so an operation absent from its
    own manifest refuses exactly like an absent capability.
    """

    capability: RuntimeCapability
    subjects: frozenset[SubjectIdentity] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.capability, RuntimeCapability):
            raise TypeError("an attested capability uses the closed vocabulary")

    def proves(self, subject: CapabilitySubject | None) -> bool:
        return subject is None or subject.identity in self.subjects


@dataclass(frozen=True)
class AttestedCapabilities:
    """The readable half of one runtime capability revision: what a build proved."""

    entries: tuple[CapabilityAttestation, ...] = ()

    def __post_init__(self) -> None:
        attested = tuple(entry.capability for entry in self.entries)
        if len(set(attested)) != len(attested):
            raise ValueError(
                "a runtime capability revision attests each capability once"
            )
        object.__setattr__(
            self,
            "entries",
            tuple(sorted(self.entries, key=lambda entry: entry.capability.value)),
        )

    def entry(self, capability: RuntimeCapability) -> CapabilityAttestation | None:
        for entry in self.entries:
            if entry.capability is capability:
                return entry
        return None


class ExecutabilityRefusalReason(StrEnum):
    """Why one requirement is unproven, as a stable token."""

    UNATTESTED_CAPABILITY = "unattested_capability"
    UNATTESTED_SUBJECT = "unattested_subject"


@dataclass(frozen=True, slots=True)
class ExecutabilityRefusal:
    """One requirement the bound capability revision does not prove."""

    reason: ExecutabilityRefusalReason
    requirement: CapabilityRequirement
    detail: str

    def __str__(self) -> str:
        return f"{self.requirement}: {self.detail} [{self.reason.value}]"


@dataclass(frozen=True)
class Executable:
    """Every capability the bound document demands is attested."""


@dataclass(frozen=True)
class NotExecutable:
    """Every unproven requirement, in the order the document demanded them.

    The whole run is refused, never a prefix of it: a graph executed down to its
    first unsupported node would leave receipts for a shape nobody accepted.
    """

    refusals: tuple[ExecutabilityRefusal, ...]

    def __post_init__(self) -> None:
        if not self.refusals:
            raise ValueError(
                "a refused document names at least one unproven requirement"
            )


type ExecutabilityDecision = Executable | NotExecutable


class RoleBindingRefusalReason(StrEnum):
    """Why one agent node's role cannot yield an agent-execution requirement."""

    UNBOUND_ROLE = "unbound_role"
    INCOMPATIBLE_EXECUTION_MODE = "incompatible_execution_mode"


@dataclass(frozen=True, slots=True)
class RoleBindingRefusal:
    """The named subject of one role refusal: which node, and which role."""

    reason: RoleBindingRefusalReason
    site: ReferenceSite
    role: str
    detail: str

    def __str__(self) -> str:
        return f"{self.site}: {self.detail} [{self.reason.value}]"


class RoleBindingRefused(Exception):
    """One agent node names a role the run binds to no agent-configuration revision."""

    def __init__(self, refusal: RoleBindingRefusal) -> None:
        super().__init__(str(refusal))
        self.refusal = refusal


@dataclass(frozen=True, slots=True)
class SkillContents:
    """What one published skill revision carries: the tool grants it brings with it.

    A skill is a capability bundle, so each grant it carries is a requirement of its
    own. The author never restates those grants in node YAML, which is why they are
    read from the published revision instead.
    """

    revision: str
    carried_tool_grants: tuple[VersionedReference, ...] = ()

    def __post_init__(self) -> None:
        if not self.revision:
            raise ValueError("skill contents name the revision they were read from")


@dataclass(frozen=True)
class PublishedSkills:
    """The read contents of the skill revisions a document pins, keyed by revision."""

    contents: tuple[SkillContents, ...] = ()

    def __post_init__(self) -> None:
        revisions = tuple(entry.revision for entry in self.contents)
        if len(set(revisions)) != len(revisions):
            raise ValueError("one skill revision has exactly one set of contents")

    def carried_tool_grants(
        self, skill: VersionedReference
    ) -> tuple[VersionedReference, ...]:
        """The grants one pinned skill carries, or a loud refusal to guess at them.

        Answering "none" for a skill nobody read would silently drop every grant that
        skill installs, which is the one failure this contract exists to prevent.
        """
        for entry in self.contents:
            if entry.revision == skill.revision:
                return entry.carried_tool_grants
        raise ValueError(
            f"skill revision {skill.revision!r} is pinned, but its carried tool "
            "grants were never read"
        )


def required_capabilities(
    document: WorkflowGraphV3,
    subworkflows: SubworkflowBinding,
    agent_bindings: tuple[ResolvedAgentBinding, ...],
    skills: PublishedSkills,
) -> tuple[CapabilityRequirement, ...]:
    """Every capability one bound document demands, over the closure it references.

    Requirements are transitive: a bound child contributes its own whole set, each
    requirement naming the chain it entered through, and a bound skill's carried
    grants enter under `tool_grants` exactly as a node's own `tools` entry does.

    The role matrix is read as the run bound it; roles are unique in every container
    that produces one, so an ambiguous matrix is refused before it reaches here.
    """
    return tuple(
        _required_of(document, subworkflows.subworkflows, agent_bindings, skills, ())
    )


def decide_executability(
    requirements: Sequence[CapabilityRequirement], attested: AttestedCapabilities
) -> ExecutabilityDecision:
    """Whether one attested capability revision proves every requirement, or not one.

    A refusal carries every unproven requirement rather than the first, so an author
    learns the whole distance to a startable run in one answer.
    """
    unproven: list[ExecutabilityRefusal] = []
    for requirement in requirements:
        refusal = _refusal_for(requirement, attested)
        if refusal is not None:
            unproven.append(refusal)
    return NotExecutable(tuple(unproven)) if unproven else Executable()


def _refusal_for(
    requirement: CapabilityRequirement, attested: AttestedCapabilities
) -> ExecutabilityRefusal | None:
    capability = requirement.capability.value
    entry = attested.entry(requirement.capability)
    if entry is None:
        return ExecutabilityRefusal(
            ExecutabilityRefusalReason.UNATTESTED_CAPABILITY,
            requirement,
            f"the bound capability revision attests no {capability}",
        )
    if not entry.proves(requirement.subject):
        return ExecutabilityRefusal(
            ExecutabilityRefusalReason.UNATTESTED_SUBJECT,
            requirement,
            f"{capability} enumerates no such revision",
        )
    return None


def _required_of(
    graph: WorkflowGraphV3,
    bound: tuple[BoundSubworkflow, ...],
    agent_bindings: tuple[ResolvedAgentBinding, ...],
    skills: PublishedSkills,
    chain: ReferenceChain,
) -> Iterator[CapabilityRequirement]:
    children = {child.node_id: child for child in bound}
    scheduled = _scheduled_node_ids(graph)
    for node in graph.nodes:
        if node.id in scheduled:
            yield CapabilityRequirement(
                RuntimeCapability.DAG_SCHEDULING,
                ReferenceSite("depends_on", node.id, None, chain),
            )
        yield from _required_of_node(node, agent_bindings, skills, chain)
        if isinstance(node, SubworkflowNodeV3):
            child = children[node.id]
            yield from _required_of(
                child.child,
                child.children,
                agent_bindings,
                skills,
                chain + (child.reference,),
            )


def _scheduled_node_ids(graph: WorkflowGraphV3) -> frozenset[str]:
    """Every node carrying more than one dependency edge, into it or out of it."""
    dependents: dict[str, int] = {}
    for node in graph.nodes:
        for dependency in node.depends_on:
            dependents[dependency] = dependents.get(dependency, 0) + 1
    return frozenset(
        node.id
        for node in graph.nodes
        if len(node.depends_on) > 1 or dependents.get(node.id, 0) > 1
    )


def _required_of_node(
    node: WorkflowNodeV3,
    agent_bindings: tuple[ResolvedAgentBinding, ...],
    skills: PublishedSkills,
    chain: ReferenceChain,
) -> Iterator[CapabilityRequirement]:
    if not isinstance(node, SubworkflowNodeV3):
        for entry in node.required_context:
            yield CapabilityRequirement(
                RuntimeCapability.CONTEXT_MATERIALIZATION,
                ReferenceSite("required_context.source", node.id, entry.name, chain),
                CapabilitySubject.of(
                    RevisionKind.CONTEXT_SOURCE,
                    VersionedReference(
                        ref=entry.source.ref, revision=entry.source.revision
                    ),
                ),
            )
    if isinstance(node, AgentNodeV3):
        yield from _required_of_agent(node, agent_bindings, skills, chain)
    if isinstance(node, DeterministicNodeV3):
        yield CapabilityRequirement(
            RuntimeCapability.DETERMINISTIC_OPERATIONS,
            ReferenceSite("operation", node.id, None, chain),
            CapabilitySubject.of(RevisionKind.DETERMINISTIC_OPERATION, node.operation),
        )
    if isinstance(node, ActionNodeV3):
        yield CapabilityRequirement(
            RuntimeCapability.EXTERNAL_EFFECTS,
            ReferenceSite("operation", node.id, None, chain),
            CapabilitySubject.of(RevisionKind.ADAPTER_OPERATION, node.operation),
        )
    if isinstance(node, SubworkflowNodeV3):
        yield CapabilityRequirement(
            RuntimeCapability.SUBWORKFLOW_EXECUTION,
            ReferenceSite("workflow", node.id, None, chain),
        )


def _required_of_agent(
    node: AgentNodeV3,
    agent_bindings: tuple[ResolvedAgentBinding, ...],
    skills: PublishedSkills,
    chain: ReferenceChain,
) -> Iterator[CapabilityRequirement]:
    site = ReferenceSite("role", node.id, None, chain)
    yield CapabilityRequirement(
        RuntimeCapability.AGENT_EXECUTION,
        site,
        CapabilitySubject(
            AgentConfigurationSubject(_bound_configuration(node, agent_bindings, site)),
            node.role,
        ),
    )
    yield CapabilityRequirement(
        RuntimeCapability.OUTPUT_VALIDATION,
        ReferenceSite("outputs", node.id, None, chain),
        CapabilitySubject(OutputProfileSubject(_output_profile(node))),
    )
    for available in node.available_context:
        yield CapabilityRequirement(
            RuntimeCapability.CONTEXT_RESOLUTION,
            ReferenceSite("available_context.source", node.id, available.name, chain),
            CapabilitySubject.of(RevisionKind.CONTEXT_SOURCE, available.source),
        )
        for read_operation in available.read_operations:
            yield CapabilityRequirement(
                RuntimeCapability.CONTEXT_RESOLUTION,
                ReferenceSite(
                    "available_context.read_operations", node.id, available.name, chain
                ),
                CapabilitySubject.of(RevisionKind.READ_OPERATION, read_operation),
            )
    for skill in node.skills:
        yield CapabilityRequirement(
            RuntimeCapability.SKILL_INSTALLATION,
            ReferenceSite("skills", node.id, None, chain),
            CapabilitySubject.of(RevisionKind.SKILL, skill),
        )
        for grant in skills.carried_tool_grants(skill):
            yield CapabilityRequirement(
                RuntimeCapability.TOOL_GRANTS,
                ReferenceSite("skills", node.id, None, chain + (skill,)),
                CapabilitySubject.of(RevisionKind.TOOL, grant),
            )
    for tool in node.tools:
        yield CapabilityRequirement(
            RuntimeCapability.TOOL_GRANTS,
            ReferenceSite("tools", node.id, None, chain),
            CapabilitySubject.of(RevisionKind.TOOL, tool),
        )


def _output_profile(node: AgentNodeV3) -> OutputProfile:
    """The output shape this node's declared arity asks the core to enforce."""
    if not node.outputs:
        return OutputProfile.NO_OUTPUT
    if len(node.outputs) == 1:
        return OutputProfile.SINGLE_JSON_OUTPUT
    return OutputProfile.MULTIPLE_JSON_OUTPUTS


def _bound_configuration(
    node: AgentNodeV3,
    agent_bindings: tuple[ResolvedAgentBinding, ...],
    site: ReferenceSite,
) -> str:
    for binding in agent_bindings:
        if binding.role.value == node.role:
            _refuse_incompatible_mode(node, binding, site)
            return binding.configuration.revision_hash.value
    raise RoleBindingRefused(
        RoleBindingRefusal(
            RoleBindingRefusalReason.UNBOUND_ROLE,
            site,
            node.role,
            f"no bound agent-configuration revision carries role {node.role!r}",
        )
    )


def _refuse_incompatible_mode(
    node: AgentNodeV3, binding: ResolvedAgentBinding, site: ReferenceSite
) -> None:
    """The node declares the mode it requires; its binding declares the capability.

    Mode is never attested twice: the manifest carries no `mode` field, so this
    comparison is where a node authored for an operator meets a configuration that
    can only run headless, or the reverse — which would make a headless node's
    outputs operator-influenced without the document ever saying so.
    """
    requested = binding.configuration.requested_capability
    if requested is AgentExecutionCapability(node.mode):
        return
    raise RoleBindingRefused(
        RoleBindingRefusal(
            RoleBindingRefusalReason.INCOMPATIBLE_EXECUTION_MODE,
            site,
            node.role,
            f"this node requires a {node.mode} agent, and role {node.role!r} binds "
            f"a configuration declaring {requested.value}",
        )
    )
