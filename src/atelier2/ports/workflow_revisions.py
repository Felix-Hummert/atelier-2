from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from atelier2.contracts.runs import WorkflowRevision, WorkflowRevisionHash
from atelier2.contracts.workflows import AnyWorkflowGraph
from atelier2.contracts.workflows_v3 import AnyWorkflowDocument
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


class WorkflowDocumentParser(Protocol):
    def __call__(self, document: bytes) -> AnyWorkflowDocument: ...


class WorkflowProjectionLimit(Protocol):
    def validate_document(self, document: bytes) -> None: ...

    def validate_graph(self, graph: AnyWorkflowGraph) -> None: ...


class DurableProjectionLimit(WorkflowProjectionLimit, Protocol):
    @property
    def maximum_document_bytes(self) -> int: ...

    @property
    def maximum_field_characters(self) -> int: ...

    @property
    def maximum_payload_bytes(self) -> int: ...

    def validate_document_length(self, byte_count: int) -> None: ...

    def validate_field_length(self, character_count: int) -> None: ...

    def validate_payload_length(self, byte_count: int) -> None: ...


class ProjectionLimitExceeded(ValueError):
    pass


@dataclass(frozen=True)
class WorkflowRevisionProjection:
    revision: WorkflowRevision
    graph: AnyWorkflowGraph


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
        projection_limit: DurableProjectionLimit | None = None,
    ) -> GetWorkflowRevisionResult: ...

    def list_workflow_revisions(
        self, after: WorkflowRevisionHash | None, limit: int
    ) -> ListWorkflowRevisionsResult: ...
