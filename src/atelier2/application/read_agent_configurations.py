"""Reading published agent configuration revisions, as decisions rather than port answers.

The list is one catalog call and one translation. What it adds over calling the
port is that the result is this layer's vocabulary: a caller matches
`AgentConfigurationRevisionsListed` without importing the store's word for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.refusals import DurableStateCorrupt, ReadUnavailable
from atelier2.contracts.agents import (
    AgentConfigurationRevision,
    AgentConfigurationRevisionHash,
    AuthProfileRevision,
    AuthProfileRevisionHash,
)
from atelier2.ports.agent_configurations import AgentConfigurationCatalog
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionPage as PortAgentConfigurationRevisionPage,
)
from atelier2.ports.agent_configurations import (
    AuthProfileRevisionPage as PortAuthProfileRevisionPage,
)
from atelier2.ports.agent_configurations import (
    CatalogReadUnavailable as PortCatalogReadUnavailable,
)
from atelier2.ports.durable_runs import (
    DurableStateCorrupt as PortDurableStateCorrupt,
)


@dataclass(frozen=True)
class AgentConfigurationRevisionsListed:
    items: tuple[tuple[AgentConfigurationRevision, AuthProfileRevision], ...]
    next_after: AgentConfigurationRevisionHash | None


type ListAgentConfigurationRevisionsResult = (
    AgentConfigurationRevisionsListed | ReadUnavailable | DurableStateCorrupt
)


@dataclass(frozen=True)
class AuthProfileRevisionsListed:
    items: tuple[AuthProfileRevision, ...]
    next_after: AuthProfileRevisionHash | None


type ListAuthProfileRevisionsResult = (
    AuthProfileRevisionsListed | ReadUnavailable | DurableStateCorrupt
)


def list_agent_configuration_revisions(
    after: AgentConfigurationRevisionHash | None,
    limit: int,
    catalog: AgentConfigurationCatalog,
) -> ListAgentConfigurationRevisionsResult:
    match catalog.list_agent_configuration_revisions(after, limit):
        case PortAgentConfigurationRevisionPage(items, next_after):
            return AgentConfigurationRevisionsListed(items, next_after)
        case PortCatalogReadUnavailable(detail):
            return ReadUnavailable(detail)
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)


def list_auth_profile_revisions(
    after: AuthProfileRevisionHash | None,
    limit: int,
    catalog: AgentConfigurationCatalog,
) -> ListAuthProfileRevisionsResult:
    match catalog.list_auth_profile_revisions(after, limit):
        case PortAuthProfileRevisionPage(items, next_after):
            return AuthProfileRevisionsListed(items, next_after)
        case PortCatalogReadUnavailable(detail):
            return ReadUnavailable(detail)
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
