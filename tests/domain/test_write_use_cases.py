from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from atelier2.application.publish_agent_configurations import (
    PUBLISHED_CONFIGURATION_FORMAT,
    AgentConfigurationRevisionCollision,
    AgentConfigurationRevisionPublished,
    AgentConfigurationRevisionUnchanged,
    AgentExecutorBindingUnavailable,
    AuthProfileRevisionCollision,
    AuthProfileRevisionConflict,
    AuthProfileRevisionNotFound,
    AuthProfileRevisionPublished,
    AuthProfileRevisionUnchanged,
    UnpublishableAgentConfiguration,
    UnpublishableAuthProfile,
    publish_agent_configuration_revision,
    publish_auth_profile_revision,
)
from atelier2.application.refusals import DurableStateCorrupt, WriteUnavailable
from atelier2.application.start_published_run import (
    AuthoredAgentBinding,
    InvalidAgentBindings,
    RunCreated,
    start_published_run,
)
from atelier2.contracts.agents import AgentConfigurationRevisionFormatVersion
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCollision as PortConfigurationCollision,
)
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated as PortConfigurationCreated,
)
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionExisting as PortConfigurationExisting,
)
from atelier2.ports.agent_configurations import (
    AgentExecutorBindingUnavailable as PortExecutorBindingUnavailable,
)
from atelier2.ports.agent_configurations import (
    AuthProfileRevisionCollision as PortProfileCollision,
)
from atelier2.ports.agent_configurations import (
    AuthProfileRevisionConflict as PortProfileConflict,
)
from atelier2.ports.agent_configurations import (
    AuthProfileRevisionCreated as PortProfileCreated,
)
from atelier2.ports.agent_configurations import (
    AuthProfileRevisionExisting as PortProfileExisting,
)
from atelier2.ports.agent_configurations import (
    AuthProfileRevisionMissing as PortProfileMissing,
)
from atelier2.ports.durable_runs import DurableRunCreated, DurableWriteUnavailable
from atelier2.ports.durable_runs import (
    DurableStateCorrupt as PortDurableStateCorrupt,
)

REVISION_HASH = WorkflowRevisionHash("a" * 64)
RUN_ID = RunId("run")
STORED: Any = object()
AUTH_PROFILE: Any = object()
RUN: Any = object()


class ScriptedCatalog:
    """A catalog that answers each publication with the one answer a case scripts."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.published: list[Any] = []

    def _record(self, revision: Any) -> Any:
        self.published.append(revision)
        return self.answer

    publish_auth_profile_revision = _record
    publish_agent_configuration_revision = _record

    def auth_profile_revision(self, *arguments: Any) -> Any:
        raise AssertionError("a publication under test read the catalog back")

    def agent_configuration_revision(self, *arguments: Any) -> Any:
        raise AssertionError("a publication under test read the catalog back")


class ScriptedStarter:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.started: list[Any] = []

    def start_published(self, request: Any) -> Any:
        self.started.append(request)
        return self.answer


WRITE_REFUSALS = [
    (DurableWriteUnavailable(), WriteUnavailable()),
    (PortDurableStateCorrupt(), DurableStateCorrupt()),
]


def publish_profile(catalog: ScriptedCatalog) -> object:
    return publish_auth_profile_revision(
        "profile", 1, "anthropic", "subscription", catalog
    )


def publish_configuration(catalog: ScriptedCatalog) -> object:
    return publish_agent_configuration_revision(
        "claude", "b" * 64, "executor@1", "headless", catalog
    )


PUBLICATIONS: list[
    tuple[str, Callable[[ScriptedCatalog], object], list[tuple[Any, Any]]]
] = [
    (
        "auth-profile",
        publish_profile,
        [
            (PortProfileCreated(STORED), AuthProfileRevisionPublished(STORED)),
            (PortProfileExisting(STORED), AuthProfileRevisionUnchanged(STORED)),
            (PortProfileConflict(), AuthProfileRevisionConflict()),
            (PortProfileCollision(), AuthProfileRevisionCollision()),
            *WRITE_REFUSALS,
        ],
    ),
    (
        "agent-configuration",
        publish_configuration,
        [
            (
                PortConfigurationCreated(STORED, AUTH_PROFILE),
                AgentConfigurationRevisionPublished(STORED, AUTH_PROFILE),
            ),
            (
                PortConfigurationExisting(STORED, AUTH_PROFILE),
                AgentConfigurationRevisionUnchanged(STORED, AUTH_PROFILE),
            ),
            (PortProfileMissing(), AuthProfileRevisionNotFound()),
            (PortExecutorBindingUnavailable(), AgentExecutorBindingUnavailable()),
            (PortConfigurationCollision(), AgentConfigurationRevisionCollision()),
            *WRITE_REFUSALS,
        ],
    ),
]


@pytest.mark.proves("every-write-decision-belongs-to-a-use-case")
@pytest.mark.parametrize(
    ("publish", "port_answer", "expected"),
    [
        pytest.param(
            publish, port_answer, expected, id=f"{name}-{type(port_answer).__name__}"
        )
        for name, publish, outcomes in PUBLICATIONS
        for port_answer, expected in outcomes
    ],
)
def test_every_port_answer_of_a_publication_becomes_this_layers_own_outcome(
    publish: Callable[[ScriptedCatalog], object], port_answer: Any, expected: Any
) -> None:
    catalog = ScriptedCatalog(port_answer)

    assert publish(catalog) == expected
    assert len(catalog.published) == 1


@pytest.mark.proves("every-write-decision-belongs-to-a-use-case")
@pytest.mark.parametrize(
    ("publish", "authored"),
    [
        (publish_auth_profile_revision, ("profile", 1, "anthropic", "not-a-mode")),
        (publish_agent_configuration_revision, ("claude", "short", "e@1", "headless")),
    ],
    ids=["unknown-auth-mode", "malformed-auth-profile-hash"],
)
def test_authored_values_that_make_no_revision_refuse_before_the_catalog_is_asked(
    publish: Callable[..., Any], authored: tuple[Any, ...]
) -> None:
    """The construction is the use-case's, so its failure is an outcome, not an
    exception a caller above has to know to catch — and nothing is published."""
    catalog = ScriptedCatalog(PortProfileCreated(STORED))

    result = publish(*authored, catalog)

    assert isinstance(
        result, (UnpublishableAuthProfile, UnpublishableAgentConfiguration)
    )
    assert catalog.published == []


@pytest.mark.proves("every-write-decision-belongs-to-a-use-case")
def test_a_configuration_is_recorded_under_the_format_this_publication_decides() -> (
    None
):
    """No caller chooses the format, so the use-case owns it rather than a route."""
    catalog = ScriptedCatalog(PortConfigurationCreated(STORED, AUTH_PROFILE))

    publish_configuration(catalog)

    assert (
        catalog.published[0].revision_format_version is PUBLISHED_CONFIGURATION_FORMAT
    )
    assert PUBLISHED_CONFIGURATION_FORMAT is AgentConfigurationRevisionFormatVersion.V2


@pytest.mark.proves("every-write-decision-belongs-to-a-use-case")
def test_a_start_that_binds_no_agent_asks_for_the_run_without_a_binding_set() -> None:
    starter = ScriptedStarter(DurableRunCreated(RUN))

    assert start_published_run(RUN_ID, REVISION_HASH, None, starter) == RunCreated(RUN)
    assert not hasattr(starter.started[0], "agent_bindings")


@pytest.mark.proves("every-write-decision-belongs-to-a-use-case")
def test_a_start_whose_authored_binding_is_no_binding_refuses_before_the_store() -> (
    None
):
    """Building the durable request is part of the decision, so a role that is not
    one refuses the start in the same vocabulary as everything else — and the store
    is never asked."""
    starter = ScriptedStarter(DurableRunCreated(RUN))

    result = start_published_run(
        RUN_ID, REVISION_HASH, (AuthoredAgentBinding("", "c" * 64),), starter
    )

    assert result == InvalidAgentBindings()
    assert starter.started == []


@pytest.mark.proves("every-write-decision-belongs-to-a-use-case")
def test_a_start_that_binds_an_agent_carries_the_authored_roles_to_the_store() -> None:
    starter = ScriptedStarter(DurableRunCreated(RUN))

    start_published_run(
        RUN_ID,
        REVISION_HASH,
        (AuthoredAgentBinding("builder", "c" * 64),),
        starter,
    )

    bound = starter.started[0].agent_bindings
    assert [binding.role.value for binding in bound.bindings] == ["builder"]
