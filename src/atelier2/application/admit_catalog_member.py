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
the caller named, and nothing else. For kind WORKFLOW those bytes already live
behind the publication door (`workflow_revisions`); founding does not invent a
second hash and does not ask the caller for a hidden catalog publish.

**Founding is an admission, not a different act.** ADR 0007 Decision 3 keeps
publication and admission apart; it does not split admission in two. A V3
revision supplies the name from its published bytes; an older format needs an
explicit name because it carries none. Later V3 admissions append their own
authored names as aliases, while older formats preserve the current name.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.refusals import DurableStateCorrupt, WriteUnavailable
from atelier2.contracts.catalog_v3 import (
    CatalogActivatedAt,
    CatalogActor,
    CatalogLineageDisplayName,
    CatalogLineageId,
)
from atelier2.contracts.revisions_v3 import (
    PublishedRevision,
    PublishedRevisionHash,
    RevisionKind,
)
from atelier2.contracts.runs import WorkflowRevisionHash
from atelier2.contracts.workflow_refusals import WorkflowDocumentInvalid
from atelier2.contracts.workflows_v3 import AnyWorkflowDocument, WorkflowGraphV3
from atelier2.ports.durable_runs import (
    DurableStateCorrupt as PortDurableStateCorrupt,
)
from atelier2.ports.durable_runs import DurableWriteUnavailable
from atelier2.ports.published_revisions import (
    AdmitCatalogMemberResult,
    CatalogAdmissionExisting,
    CatalogAdmissionLineageMissing,
    CatalogAdmissionRevisionOwned,
    CatalogAdmissions,
    CatalogNameFound,
    CatalogNameMissing,
    CatalogResolver,
    FoundCatalogLineageResult,
    PublishedRevisionFound,
    PublishedRevisionMissing,
    PublishedRevisionResolver,
    PublishedRevisionsUnavailable,
)
from atelier2.ports.published_revisions import (
    DurableStateCorrupt as RegistryCorrupt,
)
from atelier2.ports.workflow_revisions import (
    ProjectionTooLarge as PortProjectionTooLarge,
)
from atelier2.ports.workflow_revisions import (
    QueryDurableStateCorrupt,
    WorkflowDocumentParser,
    WorkflowRevisionFound,
    WorkflowRevisionMissing,
    WorkflowRevisionQueries,
)
from atelier2.ports.workflow_revisions import (
    ReadUnavailable as PortReadUnavailable,
)


@dataclass(frozen=True)
class CatalogRevisionUnpublished:
    """The named revision has no published bytes, so nothing can be admitted."""

    revision_hash: PublishedRevisionHash


@dataclass(frozen=True)
class CatalogAuthoredNameRestated:
    """A V3 request tried to become a second owner of its authored name."""


@dataclass(frozen=True)
class CatalogExplicitNameRequired:
    """A format with no authored name needs the caller to provide one."""


@dataclass(frozen=True)
class CatalogDisplayNameInvalid:
    """The authored or explicit name is outside the catalog name contract."""


type FoundLineageResult = (
    FoundCatalogLineageResult
    | CatalogRevisionUnpublished
    | CatalogAuthoredNameRestated
    | CatalogExplicitNameRequired
    | CatalogDisplayNameInvalid
    | WriteUnavailable
    | DurableStateCorrupt
)
type AdmitMemberResult = (
    AdmitCatalogMemberResult
    | CatalogRevisionUnpublished
    | CatalogDisplayNameInvalid
    | WriteUnavailable
    | DurableStateCorrupt
)


def found_catalog_lineage(
    kind: RevisionKind,
    revision_hash: PublishedRevisionHash,
    explicit_display_name: CatalogLineageDisplayName | None,
    actor: CatalogActor,
    activated_at: CatalogActivatedAt,
    revisions: PublishedRevisionResolver,
    admissions: CatalogAdmissions,
    parser: WorkflowDocumentParser,
    workflows: WorkflowRevisionQueries,
) -> FoundLineageResult:
    """Admit one published revision under its format-owned name."""

    revision = _named_revision(kind, revision_hash, revisions, workflows)
    if not isinstance(revision, PublishedRevision):
        return revision
    display_name = _founding_display_name(revision, explicit_display_name, parser)
    if not isinstance(display_name, CatalogLineageDisplayName):
        return display_name
    founded = admissions.found_lineage(revision, display_name, actor, activated_at)
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
    parser: WorkflowDocumentParser,
    workflows: WorkflowRevisionQueries,
) -> AdmitMemberResult:
    """Admit one revision, appending a V3 name only its bytes authored."""

    match catalog.resolve_name(kind, lineage_id, "head"):
        case CatalogNameFound(current_display_name=current_display_name):
            pass
        case CatalogNameMissing():
            return CatalogAdmissionLineageMissing(lineage_id)
        case PublishedRevisionsUnavailable(detail):
            return WriteUnavailable(detail)
        case RegistryCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
    revision = _named_revision(kind, revision_hash, catalog, workflows)
    if not isinstance(revision, PublishedRevision):
        return revision
    display_name = _member_display_name(revision, current_display_name, parser)
    if not isinstance(display_name, CatalogLineageDisplayName):
        return display_name
    return _application_answer(
        admissions.admit_member(lineage_id, revision, display_name, actor, activated_at)
    )


type _ResolvedRevision = (
    PublishedRevision
    | CatalogRevisionUnpublished
    | WriteUnavailable
    | DurableStateCorrupt
)


def _named_revision(
    kind: RevisionKind,
    revision_hash: PublishedRevisionHash,
    revisions: PublishedRevisionResolver,
    workflows: WorkflowRevisionQueries,
) -> _ResolvedRevision:
    """Hand the catalog the exact published bytes the caller named.

    The catalog table is one publication door. For a workflow the operator
    already published through POST /workflow-revisions, those same bytes live
    in workflow_revisions under the same hash.
    """

    match revisions.resolve(kind, revision_hash):
        case PublishedRevisionFound(revision):
            return revision
        case PublishedRevisionMissing():
            pass
        case PublishedRevisionsUnavailable(detail):
            return WriteUnavailable(detail)
        case RegistryCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
    if kind is not RevisionKind.WORKFLOW:
        return CatalogRevisionUnpublished(revision_hash)
    match workflows.get_workflow_revision(WorkflowRevisionHash(revision_hash.value)):
        case WorkflowRevisionFound(projection):
            return PublishedRevision(kind, projection.revision.document)
        case WorkflowRevisionMissing():
            return CatalogRevisionUnpublished(revision_hash)
        case PortReadUnavailable(detail):
            return WriteUnavailable(detail)
        case PortProjectionTooLarge():
            return WriteUnavailable()
        case QueryDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)


type _NameRefusal = (
    CatalogAuthoredNameRestated
    | CatalogExplicitNameRequired
    | CatalogDisplayNameInvalid
    | DurableStateCorrupt
)


def _founding_display_name(
    revision: PublishedRevision,
    explicit_display_name: CatalogLineageDisplayName | None,
    parser: WorkflowDocumentParser,
) -> CatalogLineageDisplayName | _NameRefusal:
    parsed = _parsed_workflow(revision, parser)
    if isinstance(parsed, DurableStateCorrupt):
        return parsed
    if isinstance(parsed, WorkflowGraphV3):
        if explicit_display_name is not None:
            return CatalogAuthoredNameRestated()
        return _catalog_display_name(parsed.name)
    if explicit_display_name is None:
        return CatalogExplicitNameRequired()
    return explicit_display_name


def _member_display_name(
    revision: PublishedRevision,
    current_display_name: CatalogLineageDisplayName,
    parser: WorkflowDocumentParser,
) -> CatalogLineageDisplayName | CatalogDisplayNameInvalid | DurableStateCorrupt:
    parsed = _parsed_workflow(revision, parser)
    if isinstance(parsed, DurableStateCorrupt):
        return parsed
    if isinstance(parsed, WorkflowGraphV3):
        return _catalog_display_name(parsed.name)
    return current_display_name


def _parsed_workflow(
    revision: PublishedRevision, parser: WorkflowDocumentParser
) -> AnyWorkflowDocument | None | DurableStateCorrupt:
    if revision.kind is not RevisionKind.WORKFLOW:
        return None
    try:
        return parser(revision.document)
    except WorkflowDocumentInvalid:
        return DurableStateCorrupt()


def _catalog_display_name(
    value: str,
) -> CatalogLineageDisplayName | CatalogDisplayNameInvalid:
    try:
        return CatalogLineageDisplayName(value)
    except (TypeError, ValueError):
        return CatalogDisplayNameInvalid()


def _application_answer[T](
    answer: T | DurableWriteUnavailable | PortDurableStateCorrupt,
) -> T | WriteUnavailable | DurableStateCorrupt:
    """Say "not now" in this layer's word, so a caller never names the store's."""

    if isinstance(answer, DurableWriteUnavailable):
        return WriteUnavailable()
    if isinstance(answer, PortDurableStateCorrupt):
        return DurableStateCorrupt()
    return answer
