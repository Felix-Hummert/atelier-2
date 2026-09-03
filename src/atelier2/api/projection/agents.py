"""Projection of published catalog revisions onto their wire schemas."""

from __future__ import annotations

from atelier2.api.wire.resources import (
    AgentConfigurationRevisionListItemResource,
    AgentConfigurationRevisionResource,
    AuthProfileRevisionResource,
)
from atelier2.contracts.agents import (
    AgentConfigurationRevision,
    AgentConfigurationRevisionListItem,
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


def agent_configuration_revision_list_item_resource(
    item: AgentConfigurationRevisionListItem,
) -> AgentConfigurationRevisionListItemResource:
    revision = agent_configuration_revision_resource(item.revision, item.auth_profile)
    return AgentConfigurationRevisionListItemResource(
        **revision.model_dump(),
        startable=item.startable,
        structurally_startable=item.structurally_startable,
        not_startable_reason=(
            None
            if item.not_startable_reason is None
            else item.not_startable_reason.value
        ),
        provider_probe_problem_code=(
            None
            if item.probe_failure is None
            else item.probe_failure.problem_code.value
        ),
        provider_probe_observed_at=(
            None if item.probe_failure is None else item.probe_failure.observed_at.value
        ),
    )
