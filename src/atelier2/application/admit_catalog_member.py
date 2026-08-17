"""Name a published revision, and admit later ones into the name it holds.

**Why this exists.** The store has been able to found a lineage and admit a
member since the catalog landed, and nothing in production ever called either:
a name reached the catalog only when a test wrote the row. The read door answers
`GET /workflow-revisions/by-name/{name}` over whatever is there, so without this
the catalog was a table only its own tests could fill.

**Why here and not in the route.** Both acts are the same composition -- read the
exact published bytes the caller named, then hand them to the catalog -- and a
route that did it itself would decide what a revision is. The store owns whether
an admission is legal; this layer owns that the revision handed to it is the one
the caller named, and nothing else.

**Founding is an admission, not a different act.** ADR 0007 Decision 3 keeps
publication and admission apart; it does not split admission in two. The first
admission of a lineage takes the name, every later one joins the name already
held, and both refuse through the same vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass

from atelier2.application.refusals import WriteUnavailable
from atelier2.contracts.catalog_v3 import (
    CatalogActivatedAt,
    CatalogActor,
    CatalogLineageDisplayName,
    CatalogLineageId,
)
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.ports.durable_runs import DurableWriteUnavailable
from atelier2.ports.published_revisions import (
    AdmitCatalogMemberResult,
    CatalogAdmissionExisting,
    CatalogAdmissionLineageMissing,
    CatalogAdmissionRevisionOwned,
    CatalogAdmissions,
    CatalogNameFound,
    CatalogResolver,
    FoundCatalogLineageResult,
    PublishedRevisionFound,
    PublishedRevisionResolver,
)


@dataclass(frozen=True)
class CatalogRevisionUnpublished:
    """The named revision has no published bytes, so nothing can be admitted."""

    revision_hash: PublishedRevisionHash


type FoundLineageResult = (
    FoundCatalogLineageResult | CatalogRevisionUnpublished | WriteUnavailable
)
type AdmitMemberResult = (
    AdmitCatalogMemberResult | CatalogRevisionUnpublished | WriteUnavailable
)


def found_catalog_lineage(
    kind: RevisionKind,
    revision_hash: PublishedRevisionHash,
    display_name: CatalogLineageDisplayName,
    actor: CatalogActor,
    activated_at: CatalogActivatedAt,
    revisions: PublishedRevisionResolver,
    admissions: CatalogAdmissions,
) -> FoundLineageResult:
    """Give one published revision a name, or say why the catalog refuses it."""

    match revisions.resolve(kind, revision_hash):
        case PublishedRevisionFound(revision):
            founded = admissions.found_lineage(
                revision, display_name, actor, activated_at
            )
        case _:
            return CatalogRevisionUnpublished(revision_hash)
    # A lineage id is derived from the revision, so founding the same revision
    # twice reaches the same lineage. That is the same answer only while the name
    # asked for is the name it holds: under a different name the caller is asking
    # to rename a lineage that already owns this revision, and admission does not
    # rename.
    if (
        isinstance(founded, CatalogAdmissionExisting)
        and founded.display_name != display_name
    ):
        return CatalogAdmissionRevisionOwned(revision_hash, founded.lineage.lineage_id)
    return _application_answer(founded)


def admit_catalog_member(
    kind: RevisionKind,
    lineage_id: CatalogLineageId,
    revision_hash: PublishedRevisionHash,
    actor: CatalogActor,
    activated_at: CatalogActivatedAt,
    catalog: CatalogResolver,
    admissions: CatalogAdmissions,
) -> AdmitMemberResult:
    """Admit one published revision into a lineage that already holds a name.

    The name is read from the lineage rather than taken from the caller: the
    store appends an alias with whatever name it is handed, so a caller-supplied
    one would let an admission rename the lineage. Renaming is a different act,
    and this one does not smuggle it.
    """

    match catalog.resolve_name(kind, lineage_id, "head"):
        case CatalogNameFound(current_display_name=display_name):
            pass
        case _:
            return CatalogAdmissionLineageMissing(lineage_id)
    match catalog.resolve(kind, revision_hash):
        case PublishedRevisionFound(revision):
            return _application_answer(
                admissions.admit_member(
                    lineage_id, revision, display_name, actor, activated_at
                )
            )
        case _:
            return CatalogRevisionUnpublished(revision_hash)


def _application_answer[T](answer: T | DurableWriteUnavailable) -> T | WriteUnavailable:
    """Say "not now" in this layer's word, so a caller never names the store's."""

    if isinstance(answer, DurableWriteUnavailable):
        return WriteUnavailable()
    return answer
