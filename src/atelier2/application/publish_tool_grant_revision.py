"""Publishing a tool-grant revision: exact bytes in, the catalog's own write, hash out.

The catalog store already publishes any kind. This use-case is the door that
kind `tool` was missing: it reads the bytes against the one owner that knows
what a grant this runtime redeems is, then asks the store that already owns
the write. It does not invent a second publication.
"""

from __future__ import annotations

from dataclasses import dataclass

from atelier2.application.publish_document_revision import publish_document_revision
from atelier2.application.refusals import DurableStateCorrupt, WriteUnavailable
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.tool_grants_v3 import ToolGrantRefused, read_tool_grant_document
from atelier2.ports.published_revisions import PublishedRevisionRegistry


@dataclass(frozen=True)
class ToolGrantPublicationCreated:
    revision: PublishedRevision


@dataclass(frozen=True)
class ToolGrantPublicationExisting:
    revision: PublishedRevision


@dataclass(frozen=True)
class ToolGrantPublicationInvalid:
    verdict: ToolGrantRefused


@dataclass(frozen=True)
class ToolGrantPublicationCollision:
    pass


type PublishToolGrantRevisionResult = (
    ToolGrantPublicationCreated
    | ToolGrantPublicationExisting
    | ToolGrantPublicationInvalid
    | ToolGrantPublicationCollision
    | WriteUnavailable
    | DurableStateCorrupt
)


def publish_tool_grant_revision(
    document: bytes, registry: PublishedRevisionRegistry
) -> PublishToolGrantRevisionResult:
    verdict = read_tool_grant_document(document)
    if isinstance(verdict, ToolGrantRefused):
        return ToolGrantPublicationInvalid(verdict)
    return publish_document_revision(
        PublishedRevision(RevisionKind.TOOL, document),
        registry,
        created=ToolGrantPublicationCreated,
        existing=ToolGrantPublicationExisting,
        collision=ToolGrantPublicationCollision,
    )
