from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import pytest

from atelier2.contracts.agent_attempts import (
    AgentAttemptCancellationDisposition,
    AgentAttemptId,
    AgentAttemptReplacement,
)
from atelier2.contracts.agents import MAXIMUM_AGENT_FIELD_CHARACTERS
from atelier2.contracts.executions import (
    NodeExecutionId,
    RunEvent,
    RunEventAgentAttemptBinding,
    RunEventCancellationBinding,
    RunEventKind,
)
from atelier2.contracts.runs import RunId, WorkflowRevision


@dataclass(frozen=True)
class _EventScenario:
    kind: RunEventKind
    payload: bytes
    binding: RunEventAgentAttemptBinding | RunEventCancellationBinding
    event_hash: str
    columns: tuple[str, int, str | None, str | None, str | None, str | None]


def _event(scenario: _EventScenario) -> RunEvent:
    revision = WorkflowRevision(b"format_version: 1\nstart: agent\nnodes: []\n")
    run_id = RunId("r\N{LATIN SMALL LETTER U WITH ACUTE}n-1")
    node_id = "n\N{LATIN SMALL LETTER O WITH ACUTE}de"
    return RunEvent(
        run_id,
        revision.revision_hash,
        7,
        node_id,
        NodeExecutionId.for_node(run_id, revision.revision_hash, node_id),
        scenario.kind,
        scenario.payload,
        attempt_binding=scenario.binding,
    )


def _event_scenarios() -> tuple[_EventScenario, ...]:
    attempt_id = AgentAttemptId("b" * 64)
    replacement_attempt_id = AgentAttemptId("c" * 64)
    return (
        _EventScenario(
            RunEventKind.AGENT_COMPLETED,
            b"5",
            RunEventAgentAttemptBinding(attempt_id, 1),
            "3e2e7a04215f832950fa53430b9b97ba72bf1c3a6b39e92e6228636a11fd5422",
            (attempt_id.value, 1, None, None, None, None),
        ),
        _EventScenario(
            RunEventKind.AGENT_COMPLETED,
            b"5",
            RunEventAgentAttemptBinding(attempt_id, 2),
            "18591b478bdc3c7fd4098e6829eb114f08d0fd857e4d3eaa9b1f870a3c78b094",
            (attempt_id.value, 2, None, None, None, None),
        ),
        _EventScenario(
            RunEventKind.AGENT_CANCEL_REQUESTED,
            b"cancel",
            RunEventCancellationBinding(
                attempt_id, 1, AgentAttemptReplacement.ONE, "cancel"
            ),
            "2d3059e778897c4cb93acaa7b09f7b25681ab6dbb0f038a8adabc73cf3818313",
            (attempt_id.value, 1, "cancel", "ONE", None, None),
        ),
        _EventScenario(
            RunEventKind.AGENT_CANCELLED,
            b"cancel",
            RunEventCancellationBinding(
                attempt_id,
                1,
                AgentAttemptReplacement.ONE,
                "cancel",
                AgentAttemptCancellationDisposition.REAPED_AFTER_TERM,
            ),
            "112f1235135f51ccc8485630b146613739ea7dc796cdbe8b8d7be5e9123cd5b5",
            (attempt_id.value, 1, "cancel", "ONE", "REAPED_AFTER_TERM", None),
        ),
        _EventScenario(
            RunEventKind.AGENT_CANCELLED,
            b"cancel",
            RunEventCancellationBinding(
                attempt_id,
                1,
                AgentAttemptReplacement.ONE,
                "cancel",
                AgentAttemptCancellationDisposition.REAPED_AFTER_TERM,
                replacement_attempt_id,
            ),
            "7bcdd641430e6f9aad6a86f51d7340c0399827aa8fa8b1f96da4938a5d0882bf",
            (
                attempt_id.value,
                1,
                "cancel",
                "ONE",
                "REAPED_AFTER_TERM",
                replacement_attempt_id.value,
            ),
        ),
    )


@pytest.mark.parametrize("scenario", _event_scenarios())
def test_typed_attempt_bindings_keep_every_existing_event_hash_and_column_byte(
    scenario: _EventScenario,
) -> None:
    event = _event(scenario)
    binding = event.attempt_binding
    assert binding is not None
    cancellation = binding if isinstance(binding, RunEventCancellationBinding) else None

    assert event.event_hash.value == scenario.event_hash
    assert (
        binding.attempt_id.value,
        binding.attempt_ordinal,
        None if cancellation is None else cancellation.command_id,
        None if cancellation is None else cancellation.replacement.value,
        None
        if cancellation is None or cancellation.disposition is None
        else cancellation.disposition.value,
        None
        if cancellation is None or cancellation.replacement_attempt_id is None
        else cancellation.replacement_attempt_id.value,
    ) == scenario.columns


@pytest.mark.parametrize("ordinal", (0, 3, True))
def test_attempt_event_binding_accepts_only_the_two_exact_ordinals(
    ordinal: object,
) -> None:
    with pytest.raises(ValueError, match="ordinal"):
        RunEventAgentAttemptBinding(AgentAttemptId("b" * 64), cast(int, ordinal))


def test_attempt_event_binding_requires_a_typed_attempt_id() -> None:
    with pytest.raises(TypeError, match="attempt id"):
        RunEventAgentAttemptBinding(cast(AgentAttemptId, "b" * 64), 1)


def test_cancellation_binding_requires_typed_values_and_a_bounded_command() -> None:
    attempt_id = AgentAttemptId("b" * 64)
    with pytest.raises(TypeError, match="replacement policy"):
        RunEventCancellationBinding(
            attempt_id,
            1,
            cast(AgentAttemptReplacement, "ONE"),
            "cancel",
        )
    with pytest.raises(TypeError, match="disposition"):
        RunEventCancellationBinding(
            attempt_id,
            1,
            AgentAttemptReplacement.ONE,
            "cancel",
            cast(
                AgentAttemptCancellationDisposition,
                "REAPED_AFTER_TERM",
            ),
        )
    with pytest.raises(ValueError, match=str(MAXIMUM_AGENT_FIELD_CHARACTERS)):
        RunEventCancellationBinding(
            attempt_id,
            1,
            AgentAttemptReplacement.ONE,
            "x" * (MAXIMUM_AGENT_FIELD_CHARACTERS + 1),
        )
    with pytest.raises(ValueError, match=str(MAXIMUM_AGENT_FIELD_CHARACTERS)):
        RunEventCancellationBinding(
            attempt_id,
            1,
            AgentAttemptReplacement.ONE,
            "",
        )
    with pytest.raises(TypeError, match="replacement attempt id"):
        RunEventCancellationBinding(
            attempt_id,
            1,
            AgentAttemptReplacement.ONE,
            "cancel",
            AgentAttemptCancellationDisposition.NEVER_LAUNCHED,
            cast(AgentAttemptId, "c" * 64),
        )


@pytest.mark.parametrize(
    ("kind", "binding_case"),
    (
        (RunEventKind.AGENT_CANCEL_REQUESTED, "attempt-only"),
        (RunEventKind.AGENT_COMPLETED, "cancellation"),
        (RunEventKind.AGENT_CANCEL_REQUESTED, "terminal-cancellation"),
        (RunEventKind.AGENT_CANCELLED, "cancellation"),
    ),
)
def test_run_event_rejects_every_binding_shape_that_disagrees_with_its_kind(
    kind: RunEventKind,
    binding_case: str,
) -> None:
    attempt_id = AgentAttemptId("b" * 64)
    if binding_case == "attempt-only":
        binding: RunEventAgentAttemptBinding | RunEventCancellationBinding = (
            RunEventAgentAttemptBinding(attempt_id, 1)
        )
    else:
        binding = RunEventCancellationBinding(
            attempt_id,
            1,
            AgentAttemptReplacement.NONE,
            "cancel",
            (
                AgentAttemptCancellationDisposition.NEVER_LAUNCHED
                if binding_case == "terminal-cancellation"
                else None
            ),
        )
    scenario = _EventScenario(
        kind, b"cancel", binding, "unused", ("", 1, None, None, None, None)
    )

    with pytest.raises(ValueError, match="binding|disposition"):
        _event(scenario)
