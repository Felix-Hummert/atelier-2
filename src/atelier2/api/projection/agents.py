"""Projection of published catalog revisions onto their wire schemas."""

from __future__ import annotations

from atelier2.api.wire.resources import (
    AgentConfigurationRevisionResource,
    AuthProfileRevisionResource,
)
from atelier2.contracts.agents import (
    AgentConfigurationRevision,
    AuthProfileRevision,
)


def auth_profile_revision_resource(
    revision: AuthProfileRevision,
) -> AuthProfileRevisionResource:
    return AuthProfileRevisionResource(
        profile_id=revision.profile_id,
        revision_number=revision.revision_number,
        provider_id=revision.provider_id.value,
        auth_mode=revision.auth_mode.value,
        auth_profile_revision_hash=revision.revision_hash.value,
    )


def agent_configuration_revision_resource(
    revision: AgentConfigurationRevision,
    auth_profile: AuthProfileRevision,
) -> AgentConfigurationRevisionResource:
    return AgentConfigurationRevisionResource(
        model=revision.model,
        auth_profile_revision_hash=revision.auth_profile_revision_hash.value,
        executor_revision=revision.executor_revision.value,
        provider_id=auth_profile.provider_id.value,
        auth_mode=auth_profile.auth_mode.value,
        requested_capability=revision.requested_capability.value,
        agent_configuration_revision_hash=revision.revision_hash.value,
    )
