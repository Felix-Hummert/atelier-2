"""Reading published workflow revisions, as decisions rather than port answers.

Both reads are one port call and one translation. What they add over calling the
port is that their result is this layer's vocabulary: a caller matches
`WorkflowRevisionNotFound` without importing the store's word for it, and a new
outcome is a new member here rather than a new type leaking into every route.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.refusals import DurableStateCorrupt, ReadUnavailable
from atelier2.contracts.runs import WorkflowRevisionHash
from atelier2.ports.workflow_revisions import (
    DurableProjectionLimit,
    QueryDurableStateCorrupt,
    WorkflowRevisionFound,
    WorkflowRevisionMissing,
    WorkflowRevisionPage,
    WorkflowRevisionProjection,
    WorkflowRevisionQueries,
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
class WorkflowRevisionsListed:
    revision_hashes: tuple[WorkflowRevisionHash, ...]
    next_after: WorkflowRevisionHash | None


type GetWorkflowRevisionResult = (
    WorkflowRevisionRead
    | WorkflowRevisionNotFound
    | ReadUnavailable
    | DurableStateCorrupt
)
type ListWorkflowRevisionsResult = (
    WorkflowRevisionsListed | ReadUnavailable | DurableStateCorrupt
)


def get_workflow_revision(
    revision_hash: WorkflowRevisionHash,
    projection_limit: DurableProjectionLimit,
    queries: WorkflowRevisionQueries,
) -> GetWorkflowRevisionResult:
    match queries.get_workflow_revision(revision_hash, projection_limit):
        case WorkflowRevisionFound(projection):
            return WorkflowRevisionRead(projection)
        case WorkflowRevisionMissing():
            return WorkflowRevisionNotFound()
        case PortReadUnavailable(detail):
            return ReadUnavailable(detail)
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
        case QueryDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
