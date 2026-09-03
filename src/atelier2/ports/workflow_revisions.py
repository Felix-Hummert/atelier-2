from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from atelier2.contracts.definition_sources import RevisionProvenance
from atelier2.contracts.runs import WorkflowRevision, WorkflowRevisionHash
from atelier2.contracts.workflow_projections import (
    DescribedWorkflowRevisionPage,
    EnrichedPageBudget,
    WorkflowRevisionPage,
    WorkflowRevisionProjection,
)
from atelier2.contracts.workflows_v3 import AnyWorkflowDocument
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable


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


class DurableProjectionLimit(Protocol):
    def validate_document(self, document: bytes) -> None: ...

    def validate_graph(self, graph: AnyWorkflowDocument) -> None: ...

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
class WorkflowRevisionFound:
    """One stored revision, with where its bytes first entered the catalog.

    The origin travels with the single read for the same reason it travels
    with a listed one: a detail view that had to ask a second door would
    answer `null` wherever nobody asked.
    """

    projection: WorkflowRevisionProjection
    provenance: RevisionProvenance | None = None


@dataclass(frozen=True)
class WorkflowRevisionMissing:
    pass


@dataclass(frozen=True)
class ReadUnavailable:
    detail: str | None = None


@dataclass(frozen=True)
class ProjectionTooLarge:
    """What was stored does not fit the bound its reader was configured with.

    Kept apart from `ReadUnavailable` because the two are opposite promises: a
    store that could not answer may answer the same question later, and this one
    never will. Waiting is the right response to the first and useless for the
    second, so a caller that cannot tell them apart tells its own callers to retry
    forever.

    It carries no sentence. Which bound was configured, and how that is worded to
    whoever asked, belongs to the layer that set the bound — a port that phrased it
    would be writing someone else's answer.
    """


@dataclass(frozen=True)
class QueryDurableStateCorrupt:
    pass


type GetWorkflowRevisionResult = (
    WorkflowRevisionFound
    | WorkflowRevisionMissing
    | ReadUnavailable
    | QueryDurableStateCorrupt
    | ProjectionTooLarge
)
type ListWorkflowRevisionsResult = (
    WorkflowRevisionPage
    | ReadUnavailable
    | QueryDurableStateCorrupt
    | ProjectionTooLarge
)
type ListDescribedWorkflowRevisionsResult = (
    DescribedWorkflowRevisionPage
    | ReadUnavailable
    | QueryDurableStateCorrupt
    | ProjectionTooLarge
)


class WorkflowRevisionQueries(Protocol):
    def get_workflow_revision(
        self,
        revision_hash: WorkflowRevisionHash,
    ) -> GetWorkflowRevisionResult: ...

    def list_workflow_revisions(
        self, after: WorkflowRevisionHash | None, limit: int
    ) -> ListWorkflowRevisionsResult: ...

    def list_described_workflow_revisions(
        self,
        after: WorkflowRevisionHash | None,
        limit: int,
        budget: EnrichedPageBudget,
    ) -> ListDescribedWorkflowRevisionsResult: ...
