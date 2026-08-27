"""Whether this build runs a published document as it stands -- one answer for every asker.

The start, the detail read, the described listing and the publication answer
all ask it. Deciding it in more than one place let a listing call a revision
executable that the start then refused under the same name (#701): the read
side judged the authored form alone, the start also resolved every pinned
reference. Now both rules are applied here, in this order, and nowhere else.
The resolutions an executable document collects are exactly the references a
run configuration freezes, so the start consumes this answer rather than
resolving a second time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.refusals import DurableStateCorrupt, ReadUnavailable
from atelier2.application.resolve_references import (
    declared_through,
    resolve_declared_reference_with_revision,
)
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.contracts.run_configuration_v3 import (
    DeclaredReference,
    ReferenceRefusal,
    ReferenceRefusalReason,
    ReferenceSite,
    ResolvedReference,
)
from atelier2.contracts.tool_grants_v3 import (
    ToolGrantAccepted,
    read_tool_grant_document,
    redeems_as_platform_effect,
)
from atelier2.contracts.workflow_bindings_v3 import SubworkflowBinding
from atelier2.contracts.workflows_v3 import (
    AgentNodeV3,
    AnyWorkflowDocument,
    WorkflowGraphV3,
    what_a_v3_document_still_waits_for,
)
from atelier2.ports.published_revisions import (
    PublishedRevisionFound,
    PublishedRevisionResolver,
)


@dataclass(frozen=True)
class ExecutableDocument:
    """This build runs the document; these are the references it resolved on the way."""

    resolutions: tuple[ResolvedReference, ...] = ()


@dataclass(frozen=True)
class DocumentNotExecutable:
    """What keeps this build from starting the document, in words fit for its author.

    `refusal` is the reference that did not resolve, when that is the reason;
    a form nothing binds carries none.
    """

    reason: str
    refusal: ReferenceRefusal | None = None


type Executability = (
    ExecutableDocument | DocumentNotExecutable | ReadUnavailable | DurableStateCorrupt
)


def evaluate_executability(
    graph: AnyWorkflowDocument, resolver: PublishedRevisionResolver
) -> Executability:
    """The two rules, in order: every authored form is bound, every reference resolves.

    A V1 or V2 document is executed whole and pins nothing, so it is executable
    without a lookup. A V3 document is refused at the first thing that stands in
    the way, because the author fixes one thing at a time and the start can
    admit nothing before all of them are fixed.
    """
    if not isinstance(graph, WorkflowGraphV3):
        return ExecutableDocument()
    waiting = what_a_v3_document_still_waits_for(graph)
    if waiting is not None:
        return DocumentNotExecutable(waiting)
    resolved = resolve_document_references(graph, resolver)
    if not isinstance(resolved, ExecutableDocument):
        return resolved
    refusal = _looped_platform_effect_grant_refusal(graph, resolver)
    return resolved if refusal is None else DocumentNotExecutable(refusal)


def _looped_platform_effect_grant_refusal(
    graph: WorkflowGraphV3, resolver: PublishedRevisionResolver
) -> str | None:
    """Name a loop member whose grant needs a round-aware external marker.

    The generic effect key already carries the round, but GitHub's durable
    idempotency marker is the canonical request hash. Repeated identical output
    would therefore read back the prior round's pull request as this round's
    receipt. The start refuses before it writes a run or an external effect.
    """
    for node in graph.nodes:
        if not isinstance(node, AgentNodeV3):
            continue
        loop = graph.loop_of(node.id)
        if loop is None:
            continue
        for reference in node.tools:
            try:
                revision_hash = PublishedRevisionHash(reference.revision)
            except ValueError:
                continue
            resolved = resolver.resolve(RevisionKind.TOOL, revision_hash)
            if not isinstance(resolved, PublishedRevisionFound):
                continue
            grant = read_tool_grant_document(resolved.revision.document)
            if isinstance(grant, ToolGrantAccepted) and redeems_as_platform_effect(
                grant.capability
            ):
                return (
                    f"node {node.id!r} is a member of loop {loop.id!r} and pins "
                    "an effect grant; this runtime has no round-aware external "
                    "marker contract"
                )
    return None


def resolve_document_references(
    graph: WorkflowGraphV3, resolver: PublishedRevisionResolver
) -> Executability:
    """The second rule alone: every reference the document pins resolves, or the first that does not.

    Callable on its own because a document's references are bound whether or not
    every form in it is one this runtime executes yet -- the run-configuration
    snapshot is the same snapshot for both -- while the evaluation above only
    reaches here for a form it does execute.
    """
    resolutions: list[ResolvedReference] = []
    for declared in declared_through(graph, SubworkflowBinding()):
        resolution, revision = resolve_declared_reference_with_revision(
            declared, resolver
        )
        match resolution:
            case ResolvedReference():
                resolutions.append(resolution)
                if revision is not None and declared.kind is RevisionKind.TOOL:
                    grant = read_tool_grant_document(revision.document)
                    if (
                        isinstance(grant, ToolGrantAccepted)
                        and grant.operation is not None
                    ):
                        transitive = DeclaredReference(
                            ReferenceSite(
                                "operation",
                                declared.site.node,
                                chain=(*declared.site.chain, declared.reference),
                            ),
                            RevisionKind.ADAPTER_OPERATION,
                            grant.operation,
                        )
                        nested, _operation_revision = (
                            resolve_declared_reference_with_revision(
                                transitive, resolver
                            )
                        )
                        match nested:
                            case ResolvedReference():
                                resolutions.append(nested)
                            case ReferenceRefusal():
                                return DocumentNotExecutable(
                                    public_reason(nested), nested
                                )
                            case ReadUnavailable() | DurableStateCorrupt():
                                return nested
                            case _ as unreachable:
                                assert_never(unreachable)
            case ReferenceRefusal():
                return DocumentNotExecutable(public_reason(resolution), resolution)
            case ReadUnavailable() | DurableStateCorrupt():
                return resolution
            case _ as unreachable:
                assert_never(unreachable)
    return ExecutableDocument(tuple(resolutions))


def public_reason(refusal: ReferenceRefusal) -> str:
    """The refusal as the API may say it: the authored site and reference, a stable token.

    The refusal's own `detail` is for logs and tests -- it can quote a parser,
    which is not a sentence an author should have to read on a listing.
    """
    site = refusal.site
    reference = refusal.reference
    return (
        f"{site} pins {refusal.kind.value} reference "
        f"{reference.ref}@{reference.revision}: "
        f"{_WORDING[refusal.reason]} [{refusal.reason.value}]"
    )


_WORDING: dict[ReferenceRefusalReason, str] = {
    ReferenceRefusalReason.MALFORMED_REVISION: (
        "the pinned revision is not a 64-character lowercase hexadecimal hash"
    ),
    ReferenceRefusalReason.UNPUBLISHED_REVISION: (
        "no published revision of this kind carries this hash"
    ),
    ReferenceRefusalReason.REVISION_KIND_MISMATCH: (
        "this hash names a revision published under another kind"
    ),
    ReferenceRefusalReason.RESOLVED_REVISION_MISMATCH: (
        "the registry answered with a revision of another hash"
    ),
    ReferenceRefusalReason.UNUSABLE_SCHEMA_DOCUMENT: (
        "the published revision is not a schema this product enforces"
    ),
    ReferenceRefusalReason.UNREDEEMABLE_TOOL_GRANT: (
        "the published revision is not a tool grant this runtime redeems"
    ),
    ReferenceRefusalReason.UNUSABLE_BUDGET_DOCUMENT: (
        "the published revision bounds no attempt this runtime runs"
    ),
    ReferenceRefusalReason.UNUSABLE_ADAPTER_OPERATION: (
        "the published revision is not an adapter operation this runtime performs"
    ),
}
