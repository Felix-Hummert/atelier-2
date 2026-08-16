"""Exact-bytes reconstruction of an authored agent definition revision."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from atelier2.contracts.agent_definitions import AgentDefinition
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind

type AgentDefinitionParser = Callable[[bytes], AgentDefinition]
type AgentDefinitionRenderer = Callable[[AgentDefinition], bytes]


@dataclass(frozen=True)
class ReconstructedAgentDefinition:
    """One pure reconstruction of one authored definition into one exact revision."""

    definition: AgentDefinition
    revision: PublishedRevision


def reconstruct_agent_definition(
    definition_document: bytes,
    parse_agent_definition: AgentDefinitionParser,
    render_agent_definition: AgentDefinitionRenderer,
) -> ReconstructedAgentDefinition:
    """Parse, render, and package one definition as an AGENT_DEFINITION revision."""
    definition = parse_agent_definition(definition_document)
    rendered_document = render_agent_definition(definition)
    if parse_agent_definition(rendered_document) != definition:
        raise ValueError("parsed agent definition was not stable under reconstruction")
    return ReconstructedAgentDefinition(
        definition,
        PublishedRevision(RevisionKind.AGENT_DEFINITION, definition_document),
    )
