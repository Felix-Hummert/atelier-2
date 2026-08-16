from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from atelier2.contracts.runs import WorkflowRevision, WorkflowRevisionHash
from atelier2.contracts.workflows_v3 import AnyWorkflowDocument, VersionedReference
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


@dataclass(frozen=True)
class PublishedWorkflowFound:
    revision: WorkflowRevision


@dataclass(frozen=True)
class PublishedWorkflowMissing:
    pass


type ResolvePublishedWorkflowResult = PublishedWorkflowFound | PublishedWorkflowMissing


class PublishedWorkflowResolver(Protocol):
    """Read-only resolution of one versioned reference to a published revision."""

    def resolve(
        self, reference: VersionedReference
    ) -> ResolvePublishedWorkflowResult: ...


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
class WorkflowRevisionProjection:
    revision: WorkflowRevision
    graph: AnyWorkflowDocument


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
class EnrichedPageBudget:
    """What one page may spend before it stops and reports where to resume.

    A page that says what its revisions are called has to read and parse their
    documents, and those are two different costs: measured against this parser
    the parse is paid per node -- 0.66 to 1.52 ms per node, holding across a 150x
    byte range -- while the read is paid per byte. Bounding one of them leaves
    the other free, so a page spends both and stops at whichever runs out first.
    """

    maximum_nodes: int
    maximum_document_bytes: int

    def __post_init__(self) -> None:
        for name, value in self.__dict__.items():
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class DescribedWorkflowRevisionPage:
    """Revisions together with the documents they were published as, parsed.

    `next_after` is set whenever further revisions follow, whether the page ended
    on its caller's limit or on its budget, so a caller resumes the same way in
    both cases and never has to learn which bound stopped it.
    """

    items: tuple[WorkflowRevisionProjection, ...]
    next_after: WorkflowRevisionHash | None


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
