"""Publishing what an agent is, as decisions rather than port answers.

Both publications take the values an author wrote and end in this layer's own
outcome. They also own the step the route used to do before calling anything:
turning those values into the revision the catalog stores. That construction can
fail on what the author supplied, and a failure there is an outcome of the
publication — not an exception a route happens to catch.

The format version a configuration revision is recorded under is decided here for
the same reason. No caller chooses it, so it is not configuration; it is what a
publication *is* today, and it belongs with the decision rather than wired into a
route.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.refusals import DurableStateCorrupt, WriteUnavailable
from atelier2.contracts.agents import (
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutorRevision,
    AuthMode,
    AuthProfileRevision,
    AuthProfileRevisionHash,
    ProviderId,
)
from atelier2.ports.agent_configurations import AgentConfigurationCatalog
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCollision as PortAgentConfigurationRevisionCollision,
)
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated as PortAgentConfigurationRevisionCreated,
)
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionExisting as PortAgentConfigurationRevisionExisting,
)
from atelier2.ports.agent_configurations import (
    AgentExecutorBindingUnavailable as PortAgentExecutorBindingUnavailable,
)
from atelier2.ports.agent_configurations import (
    AuthProfileRevisionCollision as PortAuthProfileRevisionCollision,
)
from atelier2.ports.agent_configurations import (
    AuthProfileRevisionConflict as PortAuthProfileRevisionConflict,
)
from atelier2.ports.agent_configurations import (
    AuthProfileRevisionCreated as PortAuthProfileRevisionCreated,
)
from atelier2.ports.agent_configurations import (
    AuthProfileRevisionExisting as PortAuthProfileRevisionExisting,
)
from atelier2.ports.agent_configurations import (
    AuthProfileRevisionMissing as PortAuthProfileRevisionMissing,
)
from atelier2.ports.durable_runs import (
    DurableStateCorrupt as PortDurableStateCorrupt,
)
from atelier2.ports.durable_runs import DurableWriteUnavailable

PUBLISHED_CONFIGURATION_FORMAT = AgentConfigurationRevisionFormatVersion.V2


@dataclass(frozen=True)
class AuthProfileRevisionPublished:
    revision: AuthProfileRevision


@dataclass(frozen=True)
class AuthProfileRevisionUnchanged:
    revision: AuthProfileRevision


@dataclass(frozen=True)
class AuthProfileRevisionConflict:
    pass


@dataclass(frozen=True)
class AuthProfileRevisionCollision:
    pass


@dataclass(frozen=True)
class UnpublishableAuthProfile:
    """The authored values do not make one auth profile revision."""


type PublishAuthProfileRevisionResult = (
    AuthProfileRevisionPublished
    | AuthProfileRevisionUnchanged
    | AuthProfileRevisionConflict
    | AuthProfileRevisionCollision
    | UnpublishableAuthProfile
    | WriteUnavailable
    | DurableStateCorrupt
)


@dataclass(frozen=True)
class AgentConfigurationRevisionPublished:
    revision: AgentConfigurationRevision
    auth_profile: AuthProfileRevision


@dataclass(frozen=True)
class AgentConfigurationRevisionUnchanged:
    revision: AgentConfigurationRevision
    auth_profile: AuthProfileRevision


@dataclass(frozen=True)
class AuthProfileRevisionNotFound:
    pass


@dataclass(frozen=True)
class AgentExecutorBindingUnavailable:
    pass


@dataclass(frozen=True)
class AgentConfigurationRevisionCollision:
    pass


@dataclass(frozen=True)
class UnpublishableAgentConfiguration:
    """The authored values do not make one agent configuration revision."""


type PublishAgentConfigurationRevisionResult = (
    AgentConfigurationRevisionPublished
    | AgentConfigurationRevisionUnchanged
    | AuthProfileRevisionNotFound
    | AgentExecutorBindingUnavailable
    | AgentConfigurationRevisionCollision
    | UnpublishableAgentConfiguration
    | WriteUnavailable
    | DurableStateCorrupt
)


def publish_auth_profile_revision(
    profile_id: str,
    revision_number: int,
    provider_id: str,
    auth_mode: str,
    catalog: AgentConfigurationCatalog,
) -> PublishAuthProfileRevisionResult:
    try:
        revision = AuthProfileRevision(
            profile_id, revision_number, ProviderId(provider_id), AuthMode(auth_mode)
        )
    except (TypeError, ValueError):
        return UnpublishableAuthProfile()
    match catalog.publish_auth_profile_revision(revision):
        case PortAuthProfileRevisionCreated(stored):
            return AuthProfileRevisionPublished(stored)
        case PortAuthProfileRevisionExisting(stored):
            return AuthProfileRevisionUnchanged(stored)
        case PortAuthProfileRevisionConflict():
            return AuthProfileRevisionConflict()
        case PortAuthProfileRevisionCollision():
            return AuthProfileRevisionCollision()
        case DurableWriteUnavailable():
            return WriteUnavailable()
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)


def publish_agent_configuration_revision(
    model: str,
    auth_profile_revision_hash: str,
    executor_revision: str,
    requested_capability: str,
    catalog: AgentConfigurationCatalog,
) -> PublishAgentConfigurationRevisionResult:
    try:
        revision = AgentConfigurationRevision(
            model,
            AuthProfileRevisionHash(auth_profile_revision_hash),
            AgentExecutorRevision(executor_revision),
            AgentExecutionCapability(requested_capability),
            PUBLISHED_CONFIGURATION_FORMAT,
        )
    except (TypeError, ValueError):
        return UnpublishableAgentConfiguration()
    match catalog.publish_agent_configuration_revision(revision):
        case PortAgentConfigurationRevisionCreated(stored, auth_profile):
            return AgentConfigurationRevisionPublished(stored, auth_profile)
        case PortAgentConfigurationRevisionExisting(stored, auth_profile):
            return AgentConfigurationRevisionUnchanged(stored, auth_profile)
        case PortAuthProfileRevisionMissing():
            return AuthProfileRevisionNotFound()
        case PortAgentExecutorBindingUnavailable():
            return AgentExecutorBindingUnavailable()
        case PortAgentConfigurationRevisionCollision():
            return AgentConfigurationRevisionCollision()
        case DurableWriteUnavailable():
            return WriteUnavailable()
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
