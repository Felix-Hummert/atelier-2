"""Reading published workflow revisions, as decisions rather than port answers.

Both reads are one port call and one translation. What they add over calling the
port is that their result is this layer's vocabulary: a caller matches
`WorkflowRevisionNotFound` without importing the store's word for it, and a new
outcome is a new member here rather than a new type leaking into every route.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, assert_never, cast

from atelier2.application.refusals import (
    DurableStateCorrupt,
    ProjectionTooLarge,
    ReadUnavailable,
)
from atelier2.application.resolve_references import (
    declared_through,
    resolve_declared_reference,
)
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.contracts.run_configuration_v3 import ReferenceRefusal
from atelier2.contracts.runs import WorkflowRevisionHash
from atelier2.contracts.schemas_v3 import SchemaRefused, read_schema_document
from atelier2.contracts.workflow_bindings_v3 import SubworkflowBinding
from atelier2.contracts.workflow_projections import (
    DescribedWorkflowRevisionPage,
    EnrichedPageBudget,
    WorkflowRevisionPage,
    WorkflowRevisionProjection,
)
from atelier2.contracts.workflows_v3 import (
    AnyWorkflowDocument,
    VersionedReference,
    WaitNodeV3,
    WorkflowGraphV3,
    what_a_v3_document_still_waits_for,
)
from atelier2.ports.published_revisions import (
    PublishedRevisionFound,
    PublishedRevisionResolver,
)
from atelier2.ports.workflow_revisions import (
    ProjectionTooLarge as PortProjectionTooLarge,
)
from atelier2.ports.workflow_revisions import (
    QueryDurableStateCorrupt,
    WorkflowRevisionFound,
    WorkflowRevisionMissing,
    WorkflowRevisionQueries,
)
from atelier2.ports.workflow_revisions import (
    ReadUnavailable as PortReadUnavailable,
)


@dataclass(frozen=True)
class WaitAnswerClassification:
    """One waiting node's answer schema, classified as far as a real read may.

    This is the application layer's own answer, never the API's: the API/wire
    projection may hold a port but may not match the record kind a port
    answers with (`api-port-record-problems`, `scripts/check_architecture.py`)
    -- reading a published schema's bytes to say `boolean` or `enum` is
    exactly that kind of match, so it happens here, beside the other reader of
    references this layer already owns (`resolve_declared_reference`), and
    the API receives only this plain, portless verdict.
    """

    node_id: str
    kind: Literal["boolean", "enum", "free"]
    values: tuple[str, ...] | None = None


@dataclass(frozen=True)
class WorkflowRevisionRead:
    """One published revision, with what this build says about running it.

    `not_executable_reason` is None exactly when the start path would admit
    the document as published; otherwise it carries that path's own words.
    """

    projection: WorkflowRevisionProjection
    not_executable_reason: str | None
    wait_answer_classifications: tuple[WaitAnswerClassification, ...] = ()


@dataclass(frozen=True)
class DescribedWorkflowRevision:
    """One listed revision, judged by the same rule the detail read applies."""

    projection: WorkflowRevisionProjection
    not_executable_reason: str | None


@dataclass(frozen=True)
class WorkflowRevisionNotFound:
    pass


@dataclass(frozen=True)
class WorkflowRevisionsDescribed:
    """One page of revisions, each with the document it was published as."""

    items: tuple[DescribedWorkflowRevision, ...]
    next_after: WorkflowRevisionHash | None


@dataclass(frozen=True)
class WorkflowRevisionsListed:
    revision_hashes: tuple[WorkflowRevisionHash, ...]
    next_after: WorkflowRevisionHash | None


type GetWorkflowRevisionResult = (
    WorkflowRevisionRead
    | WorkflowRevisionNotFound
    | ReadUnavailable
    | ProjectionTooLarge
    | DurableStateCorrupt
)
type ListWorkflowRevisionsResult = (
    WorkflowRevisionsListed | ReadUnavailable | DurableStateCorrupt | ProjectionTooLarge
)
type ListDescribedWorkflowRevisionsResult = (
    WorkflowRevisionsDescribed
    | ReadUnavailable
    | DurableStateCorrupt
    | ProjectionTooLarge
)


def get_workflow_revision(
    revision_hash: WorkflowRevisionHash,
    queries: WorkflowRevisionQueries,
    resolver: PublishedRevisionResolver,
) -> GetWorkflowRevisionResult:
    """Read one published revision, saying whether this build runs it as published.

    The resolver is required because executability is not a property of the
    bytes alone: a document pins references, and whether each one resolves is
    half of the verdict the start path gives. Classifying every wait node's
    answer schema reads through the same resolver.
    """
    match queries.get_workflow_revision(revision_hash):
        case WorkflowRevisionFound(projection):
            return describe_workflow_revision(projection, resolver)
        case WorkflowRevisionMissing():
            return WorkflowRevisionNotFound()
        case PortReadUnavailable(detail):
            return ReadUnavailable(detail)
        case PortProjectionTooLarge():
            return ProjectionTooLarge()
        case QueryDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)


def describe_workflow_revision(
    projection: WorkflowRevisionProjection, resolver: PublishedRevisionResolver
) -> WorkflowRevisionRead:
    """What this build says about one stored revision: the read's and the publication's answer alike."""
    return WorkflowRevisionRead(
        projection,
        what_a_document_still_waits_for(projection.graph, resolver),
        _wait_answer_classifications(projection.graph, resolver),
    )


def what_a_document_still_waits_for(
    graph: AnyWorkflowDocument, resolver: PublishedRevisionResolver
) -> str | None:
    """What keeps this build from starting the document as published, or None.

    The start applies two rules in turn and a reader answers with both, never
    the first alone: the executable parse names the authored form no runtime
    binds, and binding the run configuration refuses the first pinned reference
    no published revision answers. A view that stopped at the form called a
    revision executable that the start then refused under the same name, and
    the conductor read one answer and got the other (#701).
    """
    if not isinstance(graph, WorkflowGraphV3):
        return None
    waiting = what_a_v3_document_still_waits_for(graph)
    if waiting is not None:
        return waiting
    for declared in declared_through(graph, SubworkflowBinding()):
        resolution = resolve_declared_reference(declared, resolver)
        if isinstance(resolution, ReferenceRefusal):
            return f"a pinned reference nothing published answers: {resolution}"
    return None


def _wait_answer_classifications(
    graph: AnyWorkflowDocument, resolver: PublishedRevisionResolver
) -> tuple[WaitAnswerClassification, ...]:
    if not isinstance(graph, WorkflowGraphV3):
        return ()
    return tuple(
        _classify_wait_answer(node, resolver)
        for node in graph.nodes
        if isinstance(node, WaitNodeV3)
    )


def _classify_wait_answer(
    node: WaitNodeV3, resolver: PublishedRevisionResolver
) -> WaitAnswerClassification:
    """One waiting node's answer schema, classified as far as a real read may.

    The failure modes are named here rather than left to fall through to
    `free` by accident: a malformed pinned hash, a revision this build cannot
    find, a revision published under a different kind, and a revision whose
    bytes are not a schema this product enforces are the same four reasons
    `resolve_declared_reference` already refuses to *bind* a run to a
    reference, and none of them is durable corruption -- a document may name a
    schema published after itself, exactly as `orders` already echoes its own
    hull unresolved on the wire. Each of those four reasons classifies `free`,
    the honest little this reader can say, rather than refuse the whole read
    over a reference nothing has bound yet.
    """
    schema = _resolved_schema_document(node.outputs[0].schema_reference, resolver)
    if isinstance(schema, dict):
        if schema.get("type") == "boolean":
            return WaitAnswerClassification(node.id, "boolean")
        enum = schema.get("enum")
        if isinstance(enum, list):
            encoded = [_json_wire_scalar(member) for member in enum]
            if all(member is not None for member in encoded):
                return WaitAnswerClassification(
                    node.id, "enum", cast(tuple[str, ...], tuple(encoded))
                )
    return WaitAnswerClassification(node.id, "free")


def _resolved_schema_document(
    reference: VersionedReference, resolver: PublishedRevisionResolver
) -> object | None:
    """This reference's own accepted schema, or None where nothing here can read one.

    A `bool` top-level schema (`true` or `false`) is a legal Draft 2020-12
    document and is returned as-is; only a `dict` carries `type` or `enum`, so
    the caller's own `isinstance(schema, dict)` is what actually distinguishes
    a classifiable schema from one this reader still declines to guess at.
    """
    try:
        revision_hash = PublishedRevisionHash(reference.revision)
    except ValueError:
        return None  # not a well-formed pinned hash at all
    resolved = resolver.resolve(RevisionKind.SCHEMA, revision_hash)
    if not isinstance(resolved, PublishedRevisionFound):
        return None  # no published schema revision carries this hash yet
    if resolved.revision.kind is not RevisionKind.SCHEMA:
        return None  # this hash names a revision of a different published kind
    verdict = read_schema_document(resolved.revision.document)
    if isinstance(verdict, SchemaRefused):
        return None  # published bytes are not a schema this product enforces
    return verdict.schema


def _json_wire_scalar(value: object) -> str | None:
    """The exact JSON text of one schema-authored scalar, or None where it is not one.

    An `enum` member that is itself a list or object is not something a
    decision button can hold, so it does not classify here -- the caller falls
    back to `free` for the whole schema rather than a button that cannot carry
    what it names.
    """
    if value is None or isinstance(value, bool | str | int):
        return json.dumps(value)
    if isinstance(value, Decimal):
        return str(value)
    return None


def list_workflow_revisions(
    after: WorkflowRevisionHash | None,
    limit: int,
    queries: WorkflowRevisionQueries,
) -> ListWorkflowRevisionsResult:
    match queries.list_workflow_revisions(after, limit):
        case WorkflowRevisionPage(revision_hashes, next_after):
            return WorkflowRevisionsListed(revision_hashes, next_after)
        case PortReadUnavailable(detail):
            return ReadUnavailable(detail)
        case PortProjectionTooLarge():
            return ProjectionTooLarge()
        case QueryDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)


def list_described_workflow_revisions(
    after: WorkflowRevisionHash | None,
    limit: int,
    budget: EnrichedPageBudget,
    queries: WorkflowRevisionQueries,
    resolver: PublishedRevisionResolver,
) -> ListDescribedWorkflowRevisionsResult:
    """One page of revisions that carries what each document says about itself.

    The budget is the composition's decision rather than the caller's, so no
    route can widen what one page is allowed to read from the store. Each item
    is judged executable by the same rule the detail read and the start apply,
    so a listing never promises a start the service then refuses.
    """

    match queries.list_described_workflow_revisions(after, limit, budget):
        case DescribedWorkflowRevisionPage(items, next_after):
            return WorkflowRevisionsDescribed(
                tuple(
                    DescribedWorkflowRevision(
                        projection,
                        what_a_document_still_waits_for(projection.graph, resolver),
                    )
                    for projection in items
                ),
                next_after,
            )
        case PortReadUnavailable(detail):
            return ReadUnavailable(detail)
        case PortProjectionTooLarge():
            return ProjectionTooLarge()
        case QueryDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
