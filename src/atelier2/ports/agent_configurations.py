from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from atelier2.contracts.agents import (
    AgentConfigurationRevision,
    AgentConfigurationRevisionHash,
    AuthProfileRevision,
    AuthProfileRevisionHash,
)
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable


@dataclass(frozen=True)
class AgentConfigurationRevisionPage:
    items: tuple[tuple[AgentConfigurationRevision, AuthProfileRevision], ...]
    next_after: AgentConfigurationRevisionHash | None


@dataclass(frozen=True)
class AuthProfileRevisionPage:
    items: tuple[AuthProfileRevision, ...]
    next_after: AuthProfileRevisionHash | None


@dataclass(frozen=True)
class CatalogReadUnavailable:
    detail: str | None = None


type ListAgentConfigurationRevisionsResult = (
    AgentConfigurationRevisionPage | CatalogReadUnavailable | DurableStateCorrupt
)

type ListAuthProfileRevisionsResult = (
    AuthProfileRevisionPage | CatalogReadUnavailable | DurableStateCorrupt
)


@dataclass(frozen=True)
class AuthProfileRevisionCreated:
    revision: AuthProfileRevision


@dataclass(frozen=True)
class AuthProfileRevisionExisting:
    revision: AuthProfileRevision


@dataclass(frozen=True)
class AuthProfileRevisionConflict:
    pass


@dataclass(frozen=True)
class AuthProfileRevisionCollision:
    pass


type PublishAuthProfileRevisionResult = (
    AuthProfileRevisionCreated
    | AuthProfileRevisionExisting
    | AuthProfileRevisionConflict
    | AuthProfileRevisionCollision
    | DurableWriteUnavailable
    | DurableStateCorrupt
)


@dataclass(frozen=True)
class AgentConfigurationRevisionCreated:
    revision: AgentConfigurationRevision
    auth_profile: AuthProfileRevision


@dataclass(frozen=True)
class AgentConfigurationRevisionExisting:
    revision: AgentConfigurationRevision
    auth_profile: AuthProfileRevision


@dataclass(frozen=True)
class AuthProfileRevisionMissing:
    pass


@dataclass(frozen=True)
class AgentExecutorBindingUnavailable:
    pass


@dataclass(frozen=True)
class AgentConfigurationRevisionCollision:
    pass


type PublishAgentConfigurationRevisionResult = (
    AgentConfigurationRevisionCreated
    | AgentConfigurationRevisionExisting
    | AuthProfileRevisionMissing
    | AgentExecutorBindingUnavailable
    | AgentConfigurationRevisionCollision
    | DurableWriteUnavailable
    | DurableStateCorrupt
)


class AgentConfigurationCatalog(Protocol):
    def publish_auth_profile_revision(
        self, revision: AuthProfileRevision
    ) -> PublishAuthProfileRevisionResult: ...

    def publish_agent_configuration_revision(
        self, revision: AgentConfigurationRevision
    ) -> PublishAgentConfigurationRevisionResult: ...

    def auth_profile_revision(
        self, revision_hash: AuthProfileRevisionHash
    ) -> AuthProfileRevision | None: ...

    def agent_configuration_revision(
        self, revision_hash: AgentConfigurationRevisionHash
    ) -> tuple[AgentConfigurationRevision, AuthProfileRevision] | None: ...

    def list_agent_configuration_revisions(
        self, after: AgentConfigurationRevisionHash | None, limit: int
    ) -> ListAgentConfigurationRevisionsResult: ...

    def list_auth_profile_revisions(
        self, after: AuthProfileRevisionHash | None, limit: int
    ) -> ListAuthProfileRevisionsResult: ...
