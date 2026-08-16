"""Reading published workflow revisions, as decisions rather than port answers.

Both reads are one port call and one translation. What they add over calling the
port is that their result is this layer's vocabulary: a caller matches
`WorkflowRevisionNotFound` without importing the store's word for it, and a new
outcome is a new member here rather than a new type leaking into every route.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.refusals import (
    DurableStateCorrupt,
    ProjectionTooLarge,
    ReadUnavailable,
)
from atelier2.contracts.runs import WorkflowRevisionHash
from atelier2.ports.workflow_revisions import (
    DescribedWorkflowRevisionPage,
    EnrichedPageBudget,
    QueryDurableStateCorrupt,
    WorkflowRevisionFound,
    WorkflowRevisionMissing,
    WorkflowRevisionPage,
    WorkflowRevisionProjection,
    WorkflowRevisionQueries,
)
from atelier2.ports.workflow_revisions import (
    ProjectionTooLarge as PortProjectionTooLarge,
)
from atelier2.ports.workflow_revisions import (
    ReadUnavailable as PortReadUnavailable,
)


@dataclass(frozen=True)
class WorkflowRevisionRead:
    projection: WorkflowRevisionProjection


@dataclass(frozen=True)
class WorkflowRevisionNotFound:
    pass


@dataclass(frozen=True)
class WorkflowRevisionsDescribed:
    """One page of revisions, each with the document it was published as."""

    items: tuple[WorkflowRevisionProjection, ...]
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
) -> GetWorkflowRevisionResult:
    match queries.get_workflow_revision(revision_hash):
        case WorkflowRevisionFound(projection):
            return WorkflowRevisionRead(projection)
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
) -> ListDescribedWorkflowRevisionsResult:
    """One page of revisions that carries what each document says about itself.

    The budget is the composition's decision rather than the caller's, so no
    route can widen what one page is allowed to read from the store.
    """

    match queries.list_described_workflow_revisions(after, limit, budget):
        case DescribedWorkflowRevisionPage(items, next_after):
            return WorkflowRevisionsDescribed(items, next_after)
        case PortReadUnavailable(detail):
            return ReadUnavailable(detail)
        case PortProjectionTooLarge():
            return ProjectionTooLarge()
        case QueryDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
