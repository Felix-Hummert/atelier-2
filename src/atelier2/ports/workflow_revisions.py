from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from atelier2.contracts.runs import WorkflowRevision, WorkflowRevisionHash
from atelier2.contracts.workflows import WorkflowGraph
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable

PROJECTION_LIMIT_DETAIL = "Durable projection exceeds configured API limits."


@dataclass(frozen=True)
class DurableRevisionCreated:
    revision: WorkflowRevision


@dataclass(frozen=True)
class DurableRevisionExisting:
    revision: WorkflowRevision


@dataclass(frozen=True)
class DurableRevisionCollision:
    pass


type DurableRevisionPublicationResult = (
    DurableRevisionCreated
    | DurableRevisionExisting
    | DurableRevisionCollision
    | DurableWriteUnavailable
    | DurableStateCorrupt
)


class WorkflowRevisionPublisher(Protocol):
    def publish(
        self, revision: WorkflowRevision
    ) -> DurableRevisionPublicationResult: ...


class WorkflowProjectionLimit(Protocol):
    def validate_document(self, document: bytes) -> None: ...

    def validate_graph(self, graph: WorkflowGraph) -> None: ...


class ProjectionLimitExceeded(ValueError):
    pass


@dataclass(frozen=True)
class WorkflowRevisionProjection:
    revision: WorkflowRevision
    graph: WorkflowGraph


@dataclass(frozen=True)
class WorkflowRevisionFound:
    projection: WorkflowRevisionProjection


@dataclass(frozen=True)
class WorkflowRevisionMissing:
    pass


@dataclass(frozen=True)
class WorkflowRevisionPage:
    revision_hashes: tuple[WorkflowRevisionHash, ...]
    next_after: WorkflowRevisionHash | None


@dataclass(frozen=True)
class ReadUnavailable:
    detail: str | None = None


@dataclass(frozen=True)
class QueryDurableStateCorrupt:
    pass


type GetWorkflowRevisionResult = (
    WorkflowRevisionFound
    | WorkflowRevisionMissing
    | ReadUnavailable
    | QueryDurableStateCorrupt
)
type ListWorkflowRevisionsResult = (
    WorkflowRevisionPage | ReadUnavailable | QueryDurableStateCorrupt
)


class WorkflowRevisionQueries(Protocol):
    def get_workflow_revision(
        self,
        revision_hash: WorkflowRevisionHash,
        projection_limit: WorkflowProjectionLimit | None = None,
    ) -> GetWorkflowRevisionResult: ...

    def list_workflow_revisions(
        self, after: WorkflowRevisionHash | None, limit: int
    ) -> ListWorkflowRevisionsResult: ...
