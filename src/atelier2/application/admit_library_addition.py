"""Add one loose document to the library in a single attributed act.

**Why this exists.** Until this door an operator holding a file had to perform
three calls to get one entry: publish the bytes, learn their hash, then name
them. Every step between them was a state a human could stop in, and the one it
stopped in most was "published but nameless" -- bytes the catalog does not list
and no name can reach. ADR 0018 §2 says one addition is one commit; this is
where that is true for a caller who has bytes and no hash.

**What one act means per kind.** A workflow's entry is its catalog lineage, so
its addition publishes and admits inside one durable transaction (the
`LibraryAdditions` port owns that atomicity). An agent definition's entry *is*
its published revision -- the library lists agents straight from the published
kind, and no lineage names them -- so its addition is the publication the agent
door already owns, called from here rather than duplicated.

**What it refuses.** The kind is decided by `classify_definition_document`, so
this door recognises exactly what the write-free recognition door recognises and
never invents a second reading. A kind the library does not hold, an
unrecognised document, and a document two markers claim are passed back as the
recognition worded them. A workflow whose bytes author no name the catalog can
file it under is refused rather than published nameless, because a nameless
publication is the half-added state this door exists to remove.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.classify_definition_document import (
    classify_definition_document,
)
from atelier2.application.publish_agent_definition_revision import (
    AgentDefinitionPublicationCollision,
    AgentDefinitionPublicationCreated,
    AgentDefinitionPublicationExisting,
    AgentDefinitionPublicationInvalid,
    publish_agent_definition_revision,
)
from atelier2.application.publish_workflow_revision import (
    PublicationInvalid,
    WorkflowPublicationLimits,
    read_publishable_workflow,
)
from atelier2.application.reconstruct_agent_definition import (
    AgentDefinitionParser,
    AgentDefinitionRenderer,
)
from atelier2.application.refusals import DurableStateCorrupt, WriteUnavailable
from atelier2.contracts.agents import ProviderId
from atelier2.contracts.catalog_v3 import (
    CatalogActivatedAt,
    CatalogActor,
    CatalogLineageDisplayName,
    CatalogLineageId,
)
from atelier2.contracts.library_recognition import (
    DocumentAmbiguous,
    DocumentNotHeld,
    DocumentUnrecognized,
    RecognizedAgentDefinition,
    RecognizedWorkflow,
)
from atelier2.contracts.revisions_v3 import PublishedRevisionHash
from atelier2.ports.durable_runs import DurableStateCorrupt as PortDurableStateCorrupt
from atelier2.ports.durable_runs import DurableWriteUnavailable
from atelier2.ports.published_revisions import (
    AddWorkflowToLibraryResult,
    CatalogAdmissionExisting,
    CatalogAdmissionKindMismatch,
    CatalogAdmissionLineageMissing,
    CatalogAdmissionNameHeld,
    CatalogAdmissionRetired,
    CatalogAdmissionRevisionOwned,
    CatalogAdmissionUnpublished,
    CatalogLineageFounded,
    CatalogLineageIdMismatch,
    CatalogMemberAdmitted,
    LibraryAdditions,
    PublishedRevisionRegistry,
)
from atelier2.ports.workflow_revisions import WorkflowDocumentParser

FOUNDING_REVISION_NUMBER = 1
"""The member number a lineage's founding revision holds (ADR 0007 Decision 3)."""


@dataclass(frozen=True)
class WorkflowInLibrary:
    """The catalog entry one added workflow document now has."""

    name: CatalogLineageDisplayName
    description: str | None
    lineage_id: CatalogLineageId
    revision_hash: PublishedRevisionHash
    revision_number: int


@dataclass(frozen=True)
class AgentDefinitionInLibrary:
    """The library entry one added agent definition now has."""

    name: str
    description: str
    provider: ProviderId
    revision_hash: PublishedRevisionHash


type LibraryEntry = WorkflowInLibrary | AgentDefinitionInLibrary


@dataclass(frozen=True)
class LibraryAdditionAdmitted:
    """This act put the entry in the library."""

    entry: LibraryEntry


@dataclass(frozen=True)
class LibraryAdditionExisting:
    """The library already held exactly this, so the act wrote nothing new."""

    entry: LibraryEntry


@dataclass(frozen=True)
class LibraryNameUnusable:
    """The document brings no name the catalog can file it under, and why."""

    reason: str


type AdmitLibraryAdditionResult = (
    LibraryAdditionAdmitted
    | LibraryAdditionExisting
    | LibraryNameUnusable
    | DocumentNotHeld
    | DocumentUnrecognized
    | DocumentAmbiguous
    | PublicationInvalid
    | AgentDefinitionPublicationInvalid
    | AgentDefinitionPublicationCollision
    | CatalogAdmissionRevisionOwned
    | CatalogAdmissionRetired
    | WriteUnavailable
    | DurableStateCorrupt
)


def admit_library_addition(
    document: bytes,
    file_name: str | None,
    actor: CatalogActor,
    activated_at: CatalogActivatedAt,
    limits: WorkflowPublicationLimits,
    parse_workflow: WorkflowDocumentParser,
    parse_agent_definition: AgentDefinitionParser,
    render_agent_definition: AgentDefinitionRenderer,
    additions: LibraryAdditions,
    registry: PublishedRevisionRegistry,
) -> AdmitLibraryAdditionResult:
    """Recognise these bytes, keep them, and answer with the entry they became."""

    recognition = classify_definition_document(
        document, file_name, parse_workflow, parse_agent_definition
    )
    match recognition:
        case RecognizedWorkflow():
            return _add_workflow(
                document,
                recognition,
                actor,
                activated_at,
                limits,
                parse_workflow,
                additions,
            )
        case RecognizedAgentDefinition():
            return _add_agent_definition(
                document,
                recognition,
                parse_agent_definition,
                render_agent_definition,
                registry,
            )
        case DocumentNotHeld() | DocumentUnrecognized() | DocumentAmbiguous():
            return recognition
        case _ as unreachable:
            assert_never(unreachable)


def _add_workflow(
    document: bytes,
    recognised: RecognizedWorkflow,
    actor: CatalogActor,
    activated_at: CatalogActivatedAt,
    limits: WorkflowPublicationLimits,
    parse_workflow: WorkflowDocumentParser,
    additions: LibraryAdditions,
) -> AdmitLibraryAdditionResult:
    display_name = _catalog_name(recognised)
    if isinstance(display_name, LibraryNameUnusable):
        return display_name
    publishable = read_publishable_workflow(document, parse_workflow, limits)
    if isinstance(publishable, PublicationInvalid):
        return publishable
    return _workflow_entry(
        additions.add_workflow(publishable.revision, display_name, actor, activated_at),
        recognised.description,
    )


def _catalog_name(
    recognised: RecognizedWorkflow,
) -> CatalogLineageDisplayName | LibraryNameUnusable:
    """The name the library files this workflow under, taken from its own bytes."""

    if recognised.name is None:
        return LibraryNameUnusable(
            f"a format_version {recognised.format_version.value} workflow authors "
            "no name, and the library files a workflow under the name its own "
            "bytes carry"
        )
    try:
        return CatalogLineageDisplayName(recognised.name)
    except (TypeError, ValueError) as refused:
        return LibraryNameUnusable(str(refused))


def _workflow_entry(
    admission: AddWorkflowToLibraryResult, description: str | None
) -> AdmitLibraryAdditionResult:
    match admission:
        case CatalogLineageFounded(lineage, revision, display_name):
            return LibraryAdditionAdmitted(
                WorkflowInLibrary(
                    display_name,
                    description,
                    lineage.lineage_id,
                    revision.revision_hash,
                    FOUNDING_REVISION_NUMBER,
                )
            )
        case CatalogMemberAdmitted(lineage, revision, revision_number, display_name):
            return LibraryAdditionAdmitted(
                WorkflowInLibrary(
                    display_name,
                    description,
                    lineage.lineage_id,
                    revision.revision_hash,
                    revision_number,
                )
            )
        case CatalogAdmissionExisting(lineage, revision, revision_number, display_name):
            return LibraryAdditionExisting(
                WorkflowInLibrary(
                    display_name,
                    description,
                    lineage.lineage_id,
                    revision.revision_hash,
                    revision_number,
                )
            )
        case CatalogAdmissionRevisionOwned() | CatalogAdmissionRetired():
            return admission
        case (
            CatalogAdmissionUnpublished()
            | CatalogAdmissionNameHeld()
            | CatalogAdmissionLineageMissing()
            | CatalogAdmissionKindMismatch()
            | CatalogLineageIdMismatch()
        ):
            # The one act publishes what it admits, into the lineage that holds
            # the name it read there. A store answering any of these disagrees
            # with what it was just handed.
            return DurableStateCorrupt()
        case DurableWriteUnavailable():
            return WriteUnavailable()
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)


def _add_agent_definition(
    document: bytes,
    recognised: RecognizedAgentDefinition,
    parse_agent_definition: AgentDefinitionParser,
    render_agent_definition: AgentDefinitionRenderer,
    registry: PublishedRevisionRegistry,
) -> AdmitLibraryAdditionResult:
    published = publish_agent_definition_revision(
        document, parse_agent_definition, render_agent_definition, registry
    )
    match published:
        case AgentDefinitionPublicationCreated(revision):
            return LibraryAdditionAdmitted(
                _agent_entry(recognised, revision.revision_hash)
            )
        case AgentDefinitionPublicationExisting(revision):
            return LibraryAdditionExisting(
                _agent_entry(recognised, revision.revision_hash)
            )
        case (
            AgentDefinitionPublicationInvalid()
            | AgentDefinitionPublicationCollision()
            | WriteUnavailable()
            | DurableStateCorrupt()
        ):
            return published
        case _ as unreachable:
            assert_never(unreachable)


def _agent_entry(
    recognised: RecognizedAgentDefinition, revision_hash: PublishedRevisionHash
) -> AgentDefinitionInLibrary:
    return AgentDefinitionInLibrary(
        recognised.name, recognised.description, recognised.provider, revision_hash
    )
