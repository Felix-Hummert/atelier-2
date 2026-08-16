from __future__ import annotations

from dataclasses import fields

import pytest

from atelier2.application.resolve_catalog_name import (
    CatalogNameInvalidPosition,
    CatalogNameLineageRetired,
    CatalogNameMissing,
    CatalogNameResolved,
    CatalogReferenceNonMember,
    CatalogReferenceResolved,
    resolve_catalog_name,
    resolve_catalog_reference,
)
from atelier2.contracts.catalog_v3 import (
    CatalogLineage,
    CatalogLineageDisplayName,
    CatalogLineageId,
)
from atelier2.contracts.revisions_v3 import (
    PublishedRevision,
    PublishedRevisionHash,
    RevisionKind,
)
from atelier2.ports.published_revisions import (
    CatalogNameFound,
    CatalogRevisionPosition,
    PublishedRevisionFound,
    PublishedRevisionMissing,
    ResolveCatalogNameResult,
    ResolvePublishedRevisionResult,
)
from atelier2.ports.published_revisions import (
    CatalogNameMissing as PortCatalogNameMissing,
)

type CatalogNameQuery = CatalogLineageId | CatalogLineageDisplayName


class ScriptedCatalogResolver:
    def __init__(
        self,
        *,
        reference_answer: ResolvePublishedRevisionResult | None = None,
        name_answers: dict[
            tuple[CatalogNameQuery, CatalogRevisionPosition], ResolveCatalogNameResult
        ]
        | None = None,
    ) -> None:
        self.reference_answer = reference_answer
        self.name_answers = name_answers or {}
        self.reference_calls: list[
            tuple[RevisionKind, CatalogLineageId, PublishedRevisionHash]
        ] = []
        self.name_calls: list[
            tuple[RevisionKind, CatalogNameQuery, CatalogRevisionPosition]
        ] = []

    def resolve(
        self, kind: RevisionKind, revision_hash: PublishedRevisionHash
    ) -> ResolvePublishedRevisionResult:
        raise AssertionError("lineage-free resolution is not part of these decisions")

    def resolve_reference(
        self,
        kind: RevisionKind,
        lineage_id: CatalogLineageId,
        revision_hash: PublishedRevisionHash,
    ) -> ResolvePublishedRevisionResult:
        self.reference_calls.append((kind, lineage_id, revision_hash))
        if self.reference_answer is None:
            raise AssertionError("no reference answer was scripted")
        return self.reference_answer

    def resolve_name(
        self,
        kind: RevisionKind,
        lineage_id_or_name: CatalogNameQuery,
        position: CatalogRevisionPosition,
    ) -> ResolveCatalogNameResult:
        self.name_calls.append((kind, lineage_id_or_name, position))
        return self.name_answers[(lineage_id_or_name, position)]


def _published_workflow(
    document: bytes = b"name: lasagne\nsteps:  []\n",
) -> PublishedRevision:
    return PublishedRevision(RevisionKind.WORKFLOW, document)


def _lineage_id(revision: PublishedRevision) -> CatalogLineageId:
    return CatalogLineage(revision.kind, revision.revision_hash).lineage_id


def test_reference_resolution_returns_the_exact_published_revision_only() -> None:
    exact_document = b"name: lasagne\ndescription:  exact source spacing\nsteps: []\n"
    revision = _published_workflow(exact_document)
    lineage_id = _lineage_id(revision)
    catalog = ScriptedCatalogResolver(reference_answer=PublishedRevisionFound(revision))

    result = resolve_catalog_reference(
        RevisionKind.WORKFLOW, lineage_id, revision.revision_hash, catalog
    )

    assert result == CatalogReferenceResolved(revision)
    assert isinstance(result, CatalogReferenceResolved)
    assert result.revision.document == exact_document
    assert result.revision.revision_hash == PublishedRevisionHash.of(exact_document)
    assert [field.name for field in fields(CatalogReferenceResolved)] == ["revision"]
    assert catalog.reference_calls == [
        (RevisionKind.WORKFLOW, lineage_id, revision.revision_hash)
    ]


@pytest.mark.parametrize("answer_kind", ["missing", "wrong_hash", "wrong_kind"])
def test_reference_resolution_refuses_a_nonmember_or_mismatched_answer(
    answer_kind: str,
) -> None:
    requested = _published_workflow()
    lineage_id = _lineage_id(requested)
    if answer_kind == "missing":
        answer: ResolvePublishedRevisionResult = PublishedRevisionMissing()
    elif answer_kind == "wrong_hash":
        answer = PublishedRevisionFound(_published_workflow(b"name: another\n"))
    else:
        answer = PublishedRevisionFound(
            PublishedRevision(RevisionKind.SKILL, requested.document)
        )
    catalog = ScriptedCatalogResolver(reference_answer=answer)

    assert resolve_catalog_reference(
        requested.kind, lineage_id, requested.revision_hash, catalog
    ) == CatalogReferenceNonMember(lineage_id, requested.revision_hash)


@pytest.mark.parametrize("position", ["head", 1, 4])
def test_name_resolution_accepts_only_head_or_an_exact_positive_position(
    position: CatalogRevisionPosition,
) -> None:
    revision = _published_workflow()
    lineage_id = _lineage_id(revision)
    display_name = CatalogLineageDisplayName("lasagne")
    answer = CatalogNameFound(
        lineage_id,
        revision.revision_hash,
        4,
        display_name,
        retired=False,
    )
    catalog = ScriptedCatalogResolver(
        reference_answer=PublishedRevisionFound(revision),
        name_answers={(lineage_id, position): answer},
    )

    assert resolve_catalog_name(
        RevisionKind.WORKFLOW, lineage_id, position, catalog
    ) == CatalogNameResolved(
        lineage_id,
        revision,
        4,
        display_name,
    )
    assert catalog.name_calls == [(RevisionKind.WORKFLOW, lineage_id, position)]
    assert catalog.reference_calls == [
        (RevisionKind.WORKFLOW, lineage_id, revision.revision_hash)
    ]


def test_historical_alias_returns_the_current_display_name() -> None:
    revision = _published_workflow()
    lineage_id = _lineage_id(revision)
    historical_name = CatalogLineageDisplayName("pasta")
    current_name = CatalogLineageDisplayName("lasagne")
    answer = CatalogNameFound(
        lineage_id,
        revision.revision_hash,
        2,
        current_name,
        retired=False,
    )
    catalog = ScriptedCatalogResolver(
        reference_answer=PublishedRevisionFound(revision),
        name_answers={(historical_name, "head"): answer},
    )

    assert resolve_catalog_name(
        RevisionKind.WORKFLOW, historical_name, "head", catalog
    ) == CatalogNameResolved(
        lineage_id,
        revision,
        2,
        current_name,
    )


@pytest.mark.parametrize("query_kind", ["lineage_id", "historical_alias"])
def test_retired_lineage_is_refused_through_its_id_or_any_alias(
    query_kind: str,
) -> None:
    revision = _published_workflow()
    lineage_id = _lineage_id(revision)
    historical_name = CatalogLineageDisplayName("pasta")
    current_name = CatalogLineageDisplayName("lasagne")
    query: CatalogNameQuery = (
        lineage_id if query_kind == "lineage_id" else historical_name
    )
    answer = CatalogNameFound(
        lineage_id,
        revision.revision_hash,
        2,
        current_name,
        retired=True,
    )
    catalog = ScriptedCatalogResolver(name_answers={(query, "head"): answer})

    assert resolve_catalog_name(
        RevisionKind.WORKFLOW, query, "head", catalog
    ) == CatalogNameLineageRetired(lineage_id, current_name)


@pytest.mark.parametrize("stored_answer", ["missing_member", "wrong_bytes"])
def test_name_resolution_refuses_when_its_named_member_does_not_resolve(
    stored_answer: str,
) -> None:
    revision = _published_workflow()
    lineage_id = _lineage_id(revision)
    display_name = CatalogLineageDisplayName("lasagne")
    reference_answer: ResolvePublishedRevisionResult = (
        PublishedRevisionMissing()
        if stored_answer == "missing_member"
        else PublishedRevisionFound(_published_workflow(b"name: other\n"))
    )
    catalog = ScriptedCatalogResolver(
        reference_answer=reference_answer,
        name_answers={
            (display_name, "head"): CatalogNameFound(
                lineage_id,
                revision.revision_hash,
                1,
                display_name,
                retired=False,
            )
        },
    )

    assert resolve_catalog_name(
        RevisionKind.WORKFLOW, display_name, "head", catalog
    ) == CatalogReferenceNonMember(lineage_id, revision.revision_hash)
    assert catalog.reference_calls == [
        (RevisionKind.WORKFLOW, lineage_id, revision.revision_hash)
    ]


def test_missing_name_preserves_the_typed_query_and_position() -> None:
    query = CatalogLineageDisplayName("unknown")
    answer = PortCatalogNameMissing(query, 3)
    catalog = ScriptedCatalogResolver(name_answers={(query, 3): answer})

    assert resolve_catalog_name(
        RevisionKind.WORKFLOW, query, 3, catalog
    ) == CatalogNameMissing(query, 3)


@pytest.mark.parametrize(
    "position",
    [True, False, 0, -1, 1.0, "latest", None],
)
def test_name_resolution_refuses_invalid_positions_before_calling_the_port(
    position: object,
) -> None:
    query = CatalogLineageDisplayName("lasagne")
    catalog = ScriptedCatalogResolver()

    assert resolve_catalog_name(
        RevisionKind.WORKFLOW, query, position, catalog
    ) == CatalogNameInvalidPosition(position)
    assert catalog.name_calls == []
