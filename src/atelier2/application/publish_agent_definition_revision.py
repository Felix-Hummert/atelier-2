"""Publishing an authored agent definition: exact bytes in, hash out.

The catalog store already publishes any kind, and `reconstruct_agent_definition`
already owns reading, stability-checking, and packaging the authored bytes as an
AGENT_DEFINITION revision. This use-case is the door that kind was missing: it
runs that one reconstruction, then asks the store that already owns the write.
It does not invent a second parse or a second publication.
"""

from __future__ import annotations

from dataclasses import dataclass

from atelier2.application.publish_document_revision import publish_document_revision
from atelier2.application.reconstruct_agent_definition import (
    AgentDefinitionParser,
    AgentDefinitionRenderer,
    reconstruct_agent_definition,
)
from atelier2.application.refusals import DurableStateCorrupt, WriteUnavailable
from atelier2.contracts.agent_definitions import AgentDefinitionRefused
from atelier2.contracts.revisions_v3 import PublishedRevision
from atelier2.ports.published_revisions import PublishedRevisionRegistry


@dataclass(frozen=True)
class AgentDefinitionPublicationCreated:
    revision: PublishedRevision


@dataclass(frozen=True)
class AgentDefinitionPublicationExisting:
    revision: PublishedRevision


@dataclass(frozen=True)
class AgentDefinitionPublicationInvalid:
    verdict: AgentDefinitionRefused


@dataclass(frozen=True)
class AgentDefinitionPublicationCollision:
    pass


type PublishAgentDefinitionRevisionResult = (
    AgentDefinitionPublicationCreated
    | AgentDefinitionPublicationExisting
    | AgentDefinitionPublicationInvalid
    | AgentDefinitionPublicationCollision
    | WriteUnavailable
    | DurableStateCorrupt
)


def publish_agent_definition_revision(
    document: bytes,
    parse_agent_definition: AgentDefinitionParser,
    render_agent_definition: AgentDefinitionRenderer,
    registry: PublishedRevisionRegistry,
) -> PublishAgentDefinitionRevisionResult:
    try:
        reconstructed = reconstruct_agent_definition(
            document, parse_agent_definition, render_agent_definition
        )
    except AgentDefinitionRefused as refused:
        return AgentDefinitionPublicationInvalid(refused)
    return publish_document_revision(
        reconstructed.revision,
        registry,
        created=AgentDefinitionPublicationCreated,
        existing=AgentDefinitionPublicationExisting,
        collision=AgentDefinitionPublicationCollision,
    )
