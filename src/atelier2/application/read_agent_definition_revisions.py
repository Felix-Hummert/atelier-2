"""Reading the agent definitions a catalog holds, named rather than hashed.

The store keeps published revisions as exact bytes under a kind, which is all a
publisher needs and nothing a reader can read. This decision is the missing
half: one page of AGENT_DEFINITION revisions, each parsed back into the
definition its author wrote, so a caller can show a name instead of a hash.

Parsing here rather than in the route follows the same rule as the described
workflow listing: reading published bytes into the document they were published
as is an application decision, and the API receives only the plain result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.reconstruct_agent_definition import AgentDefinitionParser
from atelier2.application.refusals import DurableStateCorrupt, ReadUnavailable
from atelier2.contracts.agent_definitions import AgentDefinition, AgentDefinitionRefused
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.ports.durable_runs import DurableStateCorrupt as PortDurableStateCorrupt
from atelier2.ports.published_revisions import (
    PublishedRevisionFound,
    PublishedRevisionListing,
    PublishedRevisionPage,
    PublishedRevisionResolver,
    PublishedRevisionsUnavailable,
)


@dataclass(frozen=True)
class PublishedAgentDefinition:
    """One published revision together with the definition its bytes carry."""

    revision_hash: PublishedRevisionHash
    definition: AgentDefinition


@dataclass(frozen=True)
class AgentDefinitionRevisionsListed:
    items: tuple[PublishedAgentDefinition, ...]
    next_after: PublishedRevisionHash | None


type ListAgentDefinitionRevisionsResult = (
    AgentDefinitionRevisionsListed | ReadUnavailable | DurableStateCorrupt
)


def list_agent_definition_revisions(
    after: PublishedRevisionHash | None,
    limit: int,
    listing: PublishedRevisionListing,
    parse_agent_definition: AgentDefinitionParser,
) -> ListAgentDefinitionRevisionsResult:
    """One page of published agent definitions, or a named reason for none.

    A stored revision that no longer parses is durable corruption, not an entry
    to skip: the publish door refuses anything this parser refuses, so bytes
    that got past it and cannot be read back mean the store no longer holds
    what it accepted.
    """

    match listing.list_revisions(RevisionKind.AGENT_DEFINITION, after, limit):
        case PublishedRevisionPage(revisions, next_after):
            try:
                items = tuple(
                    PublishedAgentDefinition(
                        revision.revision_hash,
                        parse_agent_definition(revision.document),
                    )
                    for revision in revisions
                )
            except AgentDefinitionRefused:
                return DurableStateCorrupt()
            return AgentDefinitionRevisionsListed(items, next_after)
        case PublishedRevisionsUnavailable(detail):
            return ReadUnavailable(detail)
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)


@dataclass(frozen=True)
class AgentDefinitionRevisionRead:
    published: PublishedAgentDefinition


@dataclass(frozen=True)
class AgentDefinitionRevisionNotFound:
    pass


type GetAgentDefinitionRevisionResult = (
    AgentDefinitionRevisionRead | AgentDefinitionRevisionNotFound | DurableStateCorrupt
)


def get_agent_definition_revision(
    revision_hash: PublishedRevisionHash,
    resolver: PublishedRevisionResolver,
    parse_agent_definition: AgentDefinitionParser,
) -> GetAgentDefinitionRevisionResult:
    """Read one published agent definition by the hash a caller already holds.

    `resolve` is lineage-free by design (ADR 0007): a hash a caller already
    holds names the revision alone, so this asks nothing more than that
    reference already carries. A hash published under another kind answers the
    same as one nobody published -- `agent-definition-revisions` speaks for the
    `AGENT_DEFINITION` kind alone, exactly as it publishes for it alone.

    A stored revision that no longer parses is durable corruption, not a miss:
    the publish door refuses anything this parser refuses, so bytes that got
    past it and cannot be read back mean the store no longer holds what it
    accepted (the same reasoning `list_agent_definition_revisions` applies).
    """

    resolved = resolver.resolve(RevisionKind.AGENT_DEFINITION, revision_hash)
    if not isinstance(resolved, PublishedRevisionFound):
        return AgentDefinitionRevisionNotFound()
    if resolved.revision.kind is not RevisionKind.AGENT_DEFINITION:
        return AgentDefinitionRevisionNotFound()
    try:
        definition = parse_agent_definition(resolved.revision.document)
    except AgentDefinitionRefused:
        return DurableStateCorrupt()
    return AgentDefinitionRevisionRead(
        PublishedAgentDefinition(resolved.revision.revision_hash, definition)
    )
