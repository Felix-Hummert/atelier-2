"""What a provider may do is decided against one bound policy revision.

ADR 0020 §3. The grant branch has no caller before the first asking provider
channel (`#1177` step 2) and is deliberately not exercised here.
"""

from __future__ import annotations

import pytest

from atelier2.contracts.agent_attempts import AgentAttemptId
from atelier2.contracts.agent_permissions import (
    PermissionAuthority,
    PermissionCorrelationId,
    PermissionEffect,
    PermissionPolicyRevision,
    PermissionRequest,
    PermissionScope,
    PermissionScopeKind,
    decide,
)
from tests.scenarios.agents import agent_attempt_execution, agent_execution_request_v2

THE_LEASE = PermissionScope(PermissionScopeKind.PATH_PREFIX, "/lease/")
ANOTHER_DIRECTORY = PermissionScope(PermissionScopeKind.PATH_PREFIX, "/etc/")
THE_TEST_COMMAND = PermissionScope(PermissionScopeKind.COMMAND_NAME, "pytest")

MAY_READ_THE_LEASE = PermissionPolicyRevision(
    frozenset({(PermissionEffect.WORKSPACE_READ, THE_LEASE)})
)


def an_attempt_id() -> AgentAttemptId:
    return agent_attempt_execution(agent_execution_request_v2()).attempt_id


def a_question(effect: PermissionEffect, scope: PermissionScope) -> PermissionRequest:
    return PermissionRequest(
        effect, scope, PermissionCorrelationId.for_call(an_attempt_id(), 1)
    )


@pytest.mark.parametrize(
    ("effect", "scope"),
    [
        pytest.param(
            PermissionEffect.WORKSPACE_WRITE, THE_LEASE, id="effect-the-policy-omits"
        ),
        pytest.param(
            PermissionEffect.WORKSPACE_READ, ANOTHER_DIRECTORY, id="another-path"
        ),
        pytest.param(
            PermissionEffect.WORKSPACE_READ, THE_TEST_COMMAND, id="another-scope-kind"
        ),
        pytest.param(PermissionEffect.NETWORK, THE_TEST_COMMAND, id="neither"),
    ],
)
def test_a_question_the_policy_does_not_state_exactly_is_refused(
    effect: PermissionEffect, scope: PermissionScope
) -> None:
    question = a_question(effect, scope)

    decision = decide(MAY_READ_THE_LEASE, question)

    assert decision.granted is False
    assert decision.correlation_id == question.correlation_id
    assert decision.policy_revision_hash == MAY_READ_THE_LEASE.revision_hash
    assert decision.authority is PermissionAuthority.POLICY


def test_the_same_permission_stated_twice_is_the_same_revision() -> None:
    """Two constructions of one permission answer with one identity."""

    grants = (
        (PermissionEffect.WORKSPACE_READ, THE_LEASE),
        (PermissionEffect.COMMAND, THE_TEST_COMMAND),
    )

    written_one_way = PermissionPolicyRevision(frozenset(grants))
    written_the_other_way = PermissionPolicyRevision(frozenset(reversed(grants)))

    assert written_one_way.revision_hash == written_the_other_way.revision_hash
    assert written_one_way.revision_hash != MAY_READ_THE_LEASE.revision_hash


def test_a_policy_that_grants_nothing_still_has_its_own_identity() -> None:
    assert (
        PermissionPolicyRevision(frozenset()).revision_hash
        != MAY_READ_THE_LEASE.revision_hash
    )


def test_one_call_of_one_attempt_always_addresses_the_same_question() -> None:
    """The id comes from durable truth, so a provider cannot spell it away."""

    attempt_id = an_attempt_id()

    assert PermissionCorrelationId.for_call(
        attempt_id, 1
    ) == PermissionCorrelationId.for_call(attempt_id, 1)


def test_each_call_of_one_attempt_is_its_own_question() -> None:
    attempt_id = an_attempt_id()

    assert PermissionCorrelationId.for_call(
        attempt_id, 1
    ) != PermissionCorrelationId.for_call(attempt_id, 2)


def test_the_same_call_of_two_attempts_are_two_questions() -> None:
    other = agent_attempt_execution(
        agent_execution_request_v2("scenario/another-run")
    ).attempt_id

    assert PermissionCorrelationId.for_call(
        an_attempt_id(), 1
    ) != PermissionCorrelationId.for_call(other, 1)


@pytest.mark.parametrize(
    "call_ordinal",
    [pytest.param(0, id="before-the-first"), pytest.param(-1, id="negative")],
)
def test_a_call_ordinal_outside_the_counted_calls_is_refused(
    call_ordinal: int,
) -> None:
    with pytest.raises(ValueError, match="counts from one"):
        PermissionCorrelationId.for_call(an_attempt_id(), call_ordinal)
