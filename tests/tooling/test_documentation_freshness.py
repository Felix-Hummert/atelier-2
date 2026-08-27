from __future__ import annotations

from pathlib import Path

import pytest

from scripts.documentation_freshness import (
    DocumentSourceWatermark,
    FreshnessReadRefused,
    FreshnessRefusalReason,
    ObservedSourceObject,
    ObservedSourceThread,
    SourceObjectKind,
    SourceObjectReference,
    SourceThreadReference,
    read_documentation_freshness,
)
from scripts.requirement_contract import read_document_source_watermarks

PROJECT_ROOT = Path(__file__).parents[2]
THREAD = SourceThreadReference("github-issue:162")
DOCUMENT = Path("docs/requirements/0004-runner-und-remote.md")
OTHER_DOCUMENT = Path("docs/requirements/0006-kontrollierte-selbstuebernahme.md")
DOCUMENT_DIGEST = "a" * 64
OTHER_DOCUMENT_DIGEST = "b" * 64
WATERMARK = SourceObjectReference(SourceObjectKind.ISSUE_BODY_REVISION, "body-v1")
ISSUE_COMMENT = SourceObjectReference(SourceObjectKind.ISSUE_COMMENT, "comment-1")
DECISION_REVISION = SourceObjectReference(
    SourceObjectKind.DECISION_REVISION, "decision-v2"
)


def observed(reference: SourceObjectReference, ordinal: int) -> ObservedSourceObject:
    return ObservedSourceObject(reference, ordinal)


def bound_watermark(
    *,
    document: Path = DOCUMENT,
    document_digest: str = DOCUMENT_DIGEST,
    source_thread: SourceThreadReference | None = THREAD,
    source_object: SourceObjectReference | None = WATERMARK,
) -> DocumentSourceWatermark:
    return DocumentSourceWatermark(
        document, document_digest, source_thread, source_object
    )


@pytest.mark.proves(
    "a-new-source-object-after-a-bound-watermark-names-the-document-as-stale"
)
@pytest.mark.parametrize(
    ("watermarks", "objects", "stale_documents", "current_documents", "unassessed"),
    [
        (
            (bound_watermark(),),
            (observed(WATERMARK, 1),),
            (),
            (DOCUMENT,),
            (),
        ),
        (
            (bound_watermark(),),
            (observed(WATERMARK, 1), observed(ISSUE_COMMENT, 2)),
            ((DOCUMENT, (ISSUE_COMMENT,)),),
            (),
            (),
        ),
        (
            (bound_watermark(),),
            (observed(WATERMARK, 1), observed(DECISION_REVISION, 2)),
            ((DOCUMENT, (DECISION_REVISION,)),),
            (),
            (),
        ),
        (
            (bound_watermark(),),
            (
                observed(WATERMARK, 1),
                observed(ISSUE_COMMENT, 2),
                observed(DECISION_REVISION, 3),
            ),
            ((DOCUMENT, (ISSUE_COMMENT, DECISION_REVISION)),),
            (),
            (),
        ),
        (
            (
                bound_watermark(),
                bound_watermark(
                    document=OTHER_DOCUMENT,
                    document_digest=OTHER_DOCUMENT_DIGEST,
                    source_object=ISSUE_COMMENT,
                ),
            ),
            (observed(WATERMARK, 1), observed(ISSUE_COMMENT, 2)),
            ((DOCUMENT, (ISSUE_COMMENT,)),),
            (OTHER_DOCUMENT,),
            (),
        ),
        (
            (bound_watermark(source_thread=None, source_object=None),),
            (),
            (),
            (),
            (DOCUMENT,),
        ),
    ],
    ids=(
        "watermark-at-snapshot-end",
        "later-issue-comment",
        "later-decision-revision",
        "ordered-later-objects",
        "different-document-watermarks",
        "unbound-document-is-unassessed",
    ),
)
def test_source_freshness_reports_only_later_objects(
    watermarks: tuple[DocumentSourceWatermark, ...],
    objects: tuple[ObservedSourceObject, ...],
    stale_documents: tuple[tuple[Path, tuple[SourceObjectReference, ...]], ...],
    current_documents: tuple[Path, ...],
    unassessed: tuple[Path, ...],
) -> None:
    report = read_documentation_freshness(
        watermarks,
        (ObservedSourceThread(THREAD, objects),),
    )

    assert (
        tuple(
            (stale.document, tuple(item.reference for item in stale.later_objects))
            for stale in report.stale_documents
        )
        == stale_documents
    )
    assert (
        tuple(item.document for item in report.current_documents) == current_documents
    )
    assert tuple(item.document for item in report.unassessed_documents) == unassessed


@pytest.mark.proves(
    "a-new-source-object-after-a-bound-watermark-names-the-document-as-stale"
)
@pytest.mark.parametrize(
    ("watermarks", "threads", "reason"),
    [
        (
            (bound_watermark(),),
            (),
            FreshnessRefusalReason.MISSING_SOURCE_SNAPSHOT,
        ),
        (
            (bound_watermark(source_object=ISSUE_COMMENT),),
            (ObservedSourceThread(THREAD, (observed(WATERMARK, 1),)),),
            FreshnessRefusalReason.UNKNOWN_WATERMARK,
        ),
        (
            (bound_watermark(),),
            (
                ObservedSourceThread(
                    THREAD,
                    (observed(WATERMARK, 1), observed(WATERMARK, 2)),
                ),
            ),
            FreshnessRefusalReason.DUPLICATE_OBJECT_IDENTITY,
        ),
        (
            (bound_watermark(),),
            (
                ObservedSourceThread(
                    THREAD,
                    (observed(WATERMARK, 2), observed(ISSUE_COMMENT, 1)),
                ),
            ),
            FreshnessRefusalReason.INCONSISTENT_ORDERING,
        ),
    ],
    ids=(
        "missing-source-snapshot",
        "unknown-watermark",
        "duplicate-source-object",
        "inconsistent-ordering",
    ),
)
def test_source_freshness_refuses_without_a_partial_report(
    watermarks: tuple[DocumentSourceWatermark, ...],
    threads: tuple[ObservedSourceThread, ...],
    reason: FreshnessRefusalReason,
) -> None:
    with pytest.raises(FreshnessReadRefused) as refused:
        read_documentation_freshness(watermarks, threads)

    assert refused.value.reason is reason


@pytest.mark.proves(
    "a-new-source-object-after-a-bound-watermark-names-the-document-as-stale"
)
def test_the_tree_binding_feeds_the_pure_reader_and_leaves_unbound_documents_unassessed() -> (
    None
):
    watermarks = read_document_source_watermarks(PROJECT_ROOT)

    bound_documents = tuple(
        item for item in watermarks if item.source_thread is not None
    )
    assert len(bound_documents) == 1
    bound = bound_documents[0]
    assert bound.source_thread is not None
    assert bound.last_observed_source_object is not None

    report = read_documentation_freshness(
        watermarks,
        (
            ObservedSourceThread(
                bound.source_thread,
                (
                    observed(bound.last_observed_source_object, 1),
                    observed(ISSUE_COMMENT, 2),
                ),
            ),
        ),
    )

    assert tuple(item.document for item in report.stale_documents) == (bound.document,)
    assert report.stale_documents[0].later_objects == (observed(ISSUE_COMMENT, 2),)
    assert report.current_documents == ()
    assert tuple(item.document for item in report.unassessed_documents) == tuple(
        item.document for item in watermarks if item.source_thread is None
    )
