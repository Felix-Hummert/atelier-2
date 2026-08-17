"""Assemble one V3 node's context package and the request that names it.

**Why this exists.** `node-execution-request/v3` and `context-package/v3` are two
of ADR 0006's four durable V3 records, and until now nothing in production built
either: every request that reached the supervised start was assembled by its
caller, and every manifest hash named bytes nobody kept. A record whose only
author is a test is a record whose meaning is decided per caller.

**Why here and not in the store.** The store's job is that the record it is
handed is whole and self-consistent. Deciding *what* a node was given is a
composition over the document and its frozen resolution matrix, which is this
layer's shape -- the same place `bind_run_configuration` already freezes that
matrix. The host that will call both is #111's, exactly as `start_named_run`
already says of itself.

**Why it never resolves a reference itself.** Every declared reference of the
document was already resolved once, into the run configuration revision the run
is bound to. Reading those resolutions rather than repeating them is what keeps
one answer: a second resolver could disagree with the snapshot the run is
bound to, and the run would carry two truths about the same reference.
"""

from __future__ import annotations

from dataclasses import dataclass

from atelier2.contracts.agents import AgentExecutionCapability
from atelier2.contracts.node_records_v3 import (
    AvailableContextGrant,
    BoundNodeRevisions,
    ContextPackageMember,
    DeclaredContextPackage,
    DeclaredOutput,
    NodeExecutionRequest,
    NodeKindV3,
    declared_context_package_of,
)
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.contracts.run_configuration_v3 import (
    ReferenceSite,
    ResolvedReference,
    RunConfigurationRevision,
)
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.contracts.workflows_v3 import (
    AgentNodeV3,
    SubworkflowNodeV3,
    WorkflowGraphV3,
    WorkflowNodeV3,
)


@dataclass(frozen=True)
class BoundNodeExecution:
    """One node's declared context package and the request that names it.

    They travel as one value because they are one decision: the request carries
    the package's hash, so a caller holding only the request could not prove what
    the hash was taken over.
    """

    context_package: DeclaredContextPackage
    request: NodeExecutionRequest


class NodeExecutionUnresolved(Exception):
    """A declared reference of this node is absent from the frozen resolution."""

    def __init__(self, site: ReferenceSite, kind: RevisionKind) -> None:
        super().__init__(
            f"{site} declares a {kind.value} reference the run configuration "
            "did not resolve, so nothing binds it"
        )
        self.site = site
        self.kind = kind


class NodeExecutionBindingUnsupported(Exception):
    """The node declares more revisions of one kind than the request can bind."""

    def __init__(self, kind: RevisionKind, declared: int) -> None:
        super().__init__(
            f"the node declares {declared} {kind.value} revisions, and one "
            "node-execution-request/v3 binds exactly one resolved id of that "
            "kind, so nothing here can carry them"
        )
        self.kind = kind
        self.declared = declared


def bind_node_execution(
    run_id: RunId,
    workflow_revision_hash: WorkflowRevisionHash,
    graph: WorkflowGraphV3,
    node_id: str,
    run_configuration: RunConfigurationRevision,
) -> BoundNodeExecution:
    """What this node was asked to do, as the two records that say it.

    Refuses rather than fills a gap: a reference the frozen resolution does not
    carry is named with its site, because a request that quietly dropped it would
    describe a smaller task than the author wrote.
    """
    if run_configuration.workflow_revision_hash != workflow_revision_hash:
        raise ValueError(
            "the run configuration was frozen for another workflow revision"
        )
    node = graph.node(node_id)
    resolution = _ResolutionMatrix(run_configuration.resolutions, node_id)
    package = declared_context_package_of(
        workflow_revision_hash,
        run_id,
        node_id,
        _members_of(node, resolution),
    )
    request = NodeExecutionRequest(
        workflow_revision_hash=workflow_revision_hash,
        run_configuration_revision_hash=run_configuration.revision_hash,
        run_id=run_id,
        node_id=node_id,
        context_package_hash=package.package_hash,
        available_context=_grants_of(node, resolution),
        kind=NodeKindV3(node.type),
        mode=(
            AgentExecutionCapability(node.mode)
            if isinstance(node, AgentNodeV3)
            else None
        ),
        inputs=(),
        bound_revisions=_bound_revisions_of(node, resolution),
        declared_outputs=_declared_outputs_of(node, resolution),
    )
    return BoundNodeExecution(package, request)


class _ResolutionMatrix:
    """The frozen resolutions of one node, addressed the way they were declared."""

    def __init__(
        self, resolutions: tuple[ResolvedReference, ...], node_id: str
    ) -> None:
        self._by_site = {
            (entry.site.field, entry.site.entry): entry
            for entry in resolutions
            if entry.site.node == node_id and not entry.site.chain
        }
        self._ordered = tuple(
            entry
            for entry in resolutions
            if entry.site.node == node_id and not entry.site.chain
        )

    def revision(
        self, field: str, kind: RevisionKind, entry: str | None = None
    ) -> PublishedRevisionHash:
        resolved = self._by_site.get((field, entry))
        if resolved is None:
            raise NodeExecutionUnresolved(ReferenceSite(field, None, entry), kind)
        return resolved.revision_hash

    def optional_revision(
        self, field: str, entry: str | None = None
    ) -> PublishedRevisionHash | None:
        resolved = self._by_site.get((field, entry))
        return None if resolved is None else resolved.revision_hash

    def all_of(
        self, field: str, entry: str | None = None
    ) -> tuple[PublishedRevisionHash, ...]:
        return tuple(
            resolved.revision_hash
            for resolved in self._ordered
            if resolved.site.field == field
            and (entry is None or resolved.site.entry == entry)
        )


def _members_of(
    node: WorkflowNodeV3, resolution: _ResolutionMatrix
) -> tuple[ContextPackageMember, ...]:
    if isinstance(node, SubworkflowNodeV3):
        return ()
    return tuple(
        ContextPackageMember(
            name=required.name,
            source_revision=resolution.revision(
                "required_context.source", RevisionKind.CONTEXT_SOURCE, required.name
            ),
            selector=required.source.selector,
        )
        for required in node.required_context
    )


def _grants_of(
    node: WorkflowNodeV3, resolution: _ResolutionMatrix
) -> tuple[AvailableContextGrant, ...]:
    if not isinstance(node, AgentNodeV3):
        return ()
    return tuple(
        AvailableContextGrant(
            name=available.name,
            source_revision=resolution.revision(
                "available_context.source", RevisionKind.CONTEXT_SOURCE, available.name
            ),
            read_operation_revisions=resolution.all_of(
                "available_context.read_operations", available.name
            ),
        )
        for available in node.available_context
    )


def _bound_revisions_of(
    node: WorkflowNodeV3, resolution: _ResolutionMatrix
) -> BoundNodeRevisions:
    """The eight resolved ids ADR 0006 binds, each absent unless the author wrote it.

    `agent_configuration` stays absent here: a V3 node names its agent by role,
    and which configuration that role reached is the run's binding matrix, not a
    reference this document declared.
    """
    return BoundNodeRevisions(
        profile=resolution.optional_revision("profile"),
        skill=_single(resolution.all_of("skills"), RevisionKind.SKILL),
        tool=_single(resolution.all_of("tools"), RevisionKind.TOOL),
        policy=resolution.optional_revision("policy"),
        budget=resolution.optional_revision("budget"),
        retry=resolution.optional_revision("retry"),
        cancellation=resolution.optional_revision("cancellation"),
    )


def _single(
    revisions: tuple[PublishedRevisionHash, ...], kind: RevisionKind
) -> PublishedRevisionHash | None:
    """The one revision a single-valued bound field carries, or a named refusal.

    `skills` and `tools` are authored sequences while ADR 0006's request binds one
    resolved id each. A node declaring more than one cannot be written down, and
    the answer is its count rather than a quieter request: binding fewer would run
    an agent under capabilities its author declared, and the receipt would carry
    no trace of the ones that went missing.
    """
    if len(revisions) > 1:
        raise NodeExecutionBindingUnsupported(kind, len(revisions))
    return revisions[0] if revisions else None


def _declared_outputs_of(
    node: WorkflowNodeV3, resolution: _ResolutionMatrix
) -> tuple[DeclaredOutput, ...]:
    return tuple(
        DeclaredOutput(
            output.name,
            resolution.revision("outputs.schema", RevisionKind.SCHEMA, output.name),
        )
        for output in node.outputs
    )
