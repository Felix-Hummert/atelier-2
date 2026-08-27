from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class SourceObjectKind(str, Enum):
    ISSUE_BODY_REVISION = "issue_body_revision"
    ISSUE_COMMENT = "issue_comment"
    DECISION_REVISION = "decision_revision"


class FreshnessRefusalReason(str, Enum):
    MISSING_SOURCE_SNAPSHOT = "missing_source_snapshot"
    UNKNOWN_WATERMARK = "unknown_watermark"
    DUPLICATE_OBJECT_IDENTITY = "duplicate_object_identity"
    INCONSISTENT_ORDERING = "inconsistent_ordering"
    DUPLICATE_DOCUMENT = "duplicate_document"
    DUPLICATE_SOURCE_THREAD = "duplicate_source_thread"
    INCOMPLETE_WATERMARK = "incomplete_watermark"


@dataclass(frozen=True, slots=True)
class FreshnessReadRefused(Exception):
    reason: FreshnessRefusalReason
    detail: str

    def __str__(self) -> str:
        return self.detail


@dataclass(frozen=True, slots=True)
class SourceThreadReference:
    identifier: str


@dataclass(frozen=True, slots=True)
class SourceObjectReference:
    kind: SourceObjectKind
    identifier: str


@dataclass(frozen=True, slots=True)
class DocumentSourceWatermark:
    document: Path
    document_digest: str
    source_thread: SourceThreadReference | None
    last_observed_source_object: SourceObjectReference | None


@dataclass(frozen=True, slots=True)
class ObservedSourceObject:
    reference: SourceObjectReference
    ordinal: int


@dataclass(frozen=True, slots=True)
class ObservedSourceThread:
    reference: SourceThreadReference
    objects: tuple[ObservedSourceObject, ...]


@dataclass(frozen=True, slots=True)
class StaleDocument:
    document: Path
    bound_document_digest: str
    source_thread: SourceThreadReference
    watermark: SourceObjectReference
    later_objects: tuple[ObservedSourceObject, ...]


@dataclass(frozen=True, slots=True)
class StaleDocumentsReport:
    stale_documents: tuple[StaleDocument, ...]
    current_documents: tuple[DocumentSourceWatermark, ...]
    unassessed_documents: tuple[DocumentSourceWatermark, ...]


def read_documentation_freshness(
    watermarks: tuple[DocumentSourceWatermark, ...],
    observed_threads: tuple[ObservedSourceThread, ...],
) -> StaleDocumentsReport:
    """Compare bound source watermarks against an in-memory source snapshot."""
    _verify_distinct_documents(watermarks)
    observed_by_thread = _index_observed_threads(observed_threads)
    stale: list[StaleDocument] = []
    current: list[DocumentSourceWatermark] = []
    unassessed: list[DocumentSourceWatermark] = []
    for document_watermark in watermarks:
        source_thread = document_watermark.source_thread
        source_object = document_watermark.last_observed_source_object
        if source_thread is None and source_object is None:
            unassessed.append(document_watermark)
            continue
        if source_thread is None or source_object is None:
            raise FreshnessReadRefused(
                FreshnessRefusalReason.INCOMPLETE_WATERMARK,
                f"{document_watermark.document} has an incomplete source watermark",
            )
        observed = observed_by_thread.get(source_thread)
        if observed is None:
            raise FreshnessReadRefused(
                FreshnessRefusalReason.MISSING_SOURCE_SNAPSHOT,
                f"{document_watermark.document} has no source snapshot for "
                f"{source_thread.identifier}",
            )
        watermark_position = _watermark_position(
            document_watermark, observed.objects, source_object
        )
        later_objects = observed.objects[watermark_position + 1 :]
        if later_objects:
            stale.append(
                StaleDocument(
                    document_watermark.document,
                    document_watermark.document_digest,
                    source_thread,
                    source_object,
                    later_objects,
                )
            )
        else:
            current.append(document_watermark)
    return StaleDocumentsReport(tuple(stale), tuple(current), tuple(unassessed))


def _verify_distinct_documents(
    watermarks: tuple[DocumentSourceWatermark, ...],
) -> None:
    seen: set[Path] = set()
    for watermark in watermarks:
        if watermark.document in seen:
            raise FreshnessReadRefused(
                FreshnessRefusalReason.DUPLICATE_DOCUMENT,
                f"{watermark.document} has multiple source watermarks",
            )
        seen.add(watermark.document)


def _index_observed_threads(
    observed_threads: tuple[ObservedSourceThread, ...],
) -> dict[SourceThreadReference, ObservedSourceThread]:
    indexed: dict[SourceThreadReference, ObservedSourceThread] = {}
    for thread in observed_threads:
        if thread.reference in indexed:
            raise FreshnessReadRefused(
                FreshnessRefusalReason.DUPLICATE_SOURCE_THREAD,
                f"source snapshot names {thread.reference.identifier} twice",
            )
        _verify_observed_order(thread)
        indexed[thread.reference] = thread
    return indexed


def _verify_observed_order(thread: ObservedSourceThread) -> None:
    previous_ordinal = 0
    identities: set[SourceObjectReference] = set()
    for observed in thread.objects:
        if observed.reference in identities:
            raise FreshnessReadRefused(
                FreshnessRefusalReason.DUPLICATE_OBJECT_IDENTITY,
                f"{thread.reference.identifier} repeats source object "
                f"{observed.reference.identifier}",
            )
        identities.add(observed.reference)
        if observed.ordinal <= previous_ordinal:
            raise FreshnessReadRefused(
                FreshnessRefusalReason.INCONSISTENT_ORDERING,
                f"{thread.reference.identifier} has inconsistent source ordering",
            )
        previous_ordinal = observed.ordinal


def _watermark_position(
    document_watermark: DocumentSourceWatermark,
    objects: tuple[ObservedSourceObject, ...],
    source_object: SourceObjectReference,
) -> int:
    for position, observed in enumerate(objects):
        if observed.reference == source_object:
            return position
    raise FreshnessReadRefused(
        FreshnessRefusalReason.UNKNOWN_WATERMARK,
        f"{document_watermark.document} has unknown source watermark "
        f"{source_object.identifier}",
    )
