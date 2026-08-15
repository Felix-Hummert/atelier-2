"""What a V3 revision will do, as one derived object a surface can draw before start.

Decision 0006 names this object: the composed preview is
`(workflow revision, run configuration, resolved registries)`, one typed projection
that the publish preview, the typed API and the cockpit render alike, so an author
sees exactly what they authored rather than three retellings of it.

Everything here is derived, never authored. The graph comes from the parsed
document, the capability demands from the executability record, and the resolved
registries from the reference binding; this module only gives that truth a shape.
Because decision 0006 stages execution rather than the format, the shape has to
carry a revision that is publishable and not yet executable: an unresolved
reference and an unproven capability are fields, not the end of the rendering.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import assert_never

from atelier2.contracts.capabilities_v3 import (
    CapabilityRequirement,
    ExecutabilityDecision,
    ExecutabilityRefusal,
)
from atelier2.contracts.run_configuration_v3 import (
    ReferenceRefusal,
    ReferenceSite,
    ResolvedReference,
)
from atelier2.contracts.runs import WorkflowRevisionHash
from atelier2.contracts.workflows_v3 import (
    ActionNodeV3,
    AgentMode,
    AgentNodeV3,
    DeterministicNodeV3,
    JoinRule,
    SubworkflowNodeV3,
    VersionedReference,
    WaitNodeV3,
    WorkflowNodeV3,
)


class ConfigurationBinding(StrEnum):
    """Whether a start command has bound the configuration this preview shows.

    Decision 0006 keeps an author's intent and a run's binding apart: while a
    configuration is proposed, an edit writes into the proposal; once bound, the
    snapshot is immutable and an edit authors a successor instead. A preview that
    did not say which it is would let a reader mistake one for the other.
    """

    PROPOSED = "proposed"
    BOUND = "bound"


class PreviewNodeKind(StrEnum):
    """The five node forms, in the tokens the document itself writes.

    The parser owns the vocabulary. `of` is the single place the rendered form is
    tied to it, and it is exhaustive over the parsed node union, so a sixth kind
    cannot be added to the language without this projection being told.
    """

    AGENT = "agent"
    DETERMINISTIC = "deterministic"
    WAIT = "wait"
    SUBWORKFLOW = "subworkflow"
    ACTION = "action"

    @classmethod
    def of(cls, node: WorkflowNodeV3) -> PreviewNodeKind:
        match node:
            case AgentNodeV3():
                return cls.AGENT
            case DeterministicNodeV3():
                return cls.DETERMINISTIC
            case WaitNodeV3():
                return cls.WAIT
            case SubworkflowNodeV3():
                return cls.SUBWORKFLOW
            case ActionNodeV3():
                return cls.ACTION
            case _ as unreachable:
                assert_never(unreachable)


@dataclass(frozen=True, slots=True)
class PreviewRole:
    """What the bound role matrix answers for one agent node.

    Decision 0006 requires the preview to show roles with the provider and model
    they are bound to, and the configuration revision is what makes that answer
    checkable rather than a label.
    """

    role: str
    provider: str
    model: str
    configuration_revision: str


@dataclass(frozen=True, slots=True)
class PreviewEdge:
    """One control edge: the dependency, and the node it releases."""

    depends_on: str
    node: str


@dataclass(frozen=True)
class PreviewChild:
    """The child one subworkflow node binds: what it named, and what that resolved to.

    Both halves are drawn, because they answer different questions. The authored
    reference is what the document says and what an author edits; the revision hash
    is which exact bytes will run, and two children whose parsed graphs look alike
    are the same picture under different bytes without it.
    """

    reference: VersionedReference
    revision_hash: str
    graph: ComposedPreviewGraph

    def __post_init__(self) -> None:
        if not self.revision_hash:
            raise ValueError("a previewed child names the revision it resolved to")


@dataclass(frozen=True)
class PreviewNode:
    """One node as a surface draws it, with what it demands and what it waits for.

    `demands` are the requirements the executability record derived for this node,
    including the ones a bound skill carries in transitively. `unproven` is the
    subset the bound capability revision does not attest — the capability this node
    is still waiting for — and it is empty for every node of an executable preview.
    """

    id: str
    kind: PreviewNodeKind
    join: JoinRule | None
    role: PreviewRole | None
    mode: AgentMode | None
    demands: tuple[CapabilityRequirement, ...]
    unproven: tuple[ExecutabilityRefusal, ...]
    child: PreviewChild | None


@dataclass(frozen=True, slots=True)
class UnknownSkillGrants:
    """A published skill this preview could not read, so its grants are unknown.

    The reference resolved — the registry carries the revision — so this is not an
    unresolved reference, and nothing is missing from the run. What is missing is
    the reading: no contents were read for it here, so the tool grants it installs
    are absent from the demands drawn on every node declaring it. A preview that
    answered "no grants" instead would draw a node as fully attested while it
    installs tools no build proved, which is the one mistake this refusal prevents.
    """

    site: ReferenceSite
    reference: VersionedReference

    def __str__(self) -> str:
        return (
            f"{self.site} skill {self.reference.ref}@{self.reference.revision}: "
            "its published contents were never read, so the tool grants it carries "
            "are unknown rather than none"
        )


@dataclass(frozen=True, slots=True)
class PreviewGraphInput:
    """One order a graph demands at start: its name, and the schema it must match.

    This is the only part of a preview that says what a caller still has to bring
    rather than what the revision will do. Without it a reader cannot tell a
    workflow that runs from one that will refuse every start until it is handed
    something — the two draw identically until a start is attempted.
    """

    name: str
    schema_reference: VersionedReference


@dataclass(frozen=True)
class ComposedPreviewGraph:
    """One document's derived graph, and the registries its references land in.

    A subworkflow node carries the child's own graph, so a reused workflow is drawn
    where it is used rather than flattened into its parent. Orders and references
    are both kept where they were declared: a child's sit in the child's graph,
    under the chain it was reached by.

    The last two fields are what this graph could not answer, each at the site that
    asked: a reference that resolved to nothing, and a skill whose grants nobody
    read. Both are drawn rather than raised, because a revision that is publishable
    and not yet executable is the state decision 0006 exists for — and because the
    executability verdict speaks only for the demands that could be derived, a
    reader learns the rest of the distance here.
    """

    graph_inputs: tuple[PreviewGraphInput, ...]
    nodes: tuple[PreviewNode, ...]
    edges: tuple[PreviewEdge, ...]
    resolved_references: tuple[ResolvedReference, ...]
    unresolved_references: tuple[ReferenceRefusal, ...]
    unknown_skill_grants: tuple[UnknownSkillGrants, ...]


@dataclass(frozen=True)
class ComposedPreview:
    """The whole renderable truth of one revision before anything of it runs."""

    workflow_revision_hash: WorkflowRevisionHash
    configuration: ConfigurationBinding
    graph: ComposedPreviewGraph
    executability: ExecutabilityDecision
