from __future__ import annotations

from dataclasses import fields
from typing import cast, get_args

import pytest

from atelier2.contracts.agent_attempts import (
    MAXIMUM_RUNNER_STANDARD_ERROR_BYTES,
    AgentAttemptCancellationDisposition,
    ProcessExitSignature,
    RunnerCancellation,
    RunnerCancellationObservation,
    RunnerInvocationLost,
    RunnerOutputLimitExceeded,
    RunnerOutputStream,
    RunnerProcessBoundaryFailure,
    RunnerProviderFailure,
    RunnerProviderResult,
    RunnerTerminalEvidence,
)
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_FIELD_CHARACTERS,
    MAXIMUM_AGENT_OUTPUT_BYTES_V2,
    MAXIMUM_SIGNED_INT64,
    AgentExecutionResult,
)
from atelier2.ports.agent_executions import (
    MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES,
    AgentProcessCompletion,
)


def test_runner_terminal_evidence_is_exactly_the_six_authoritative_endings() -> None:
    members = set(get_args(RunnerTerminalEvidence.__value__))

    assert members == {
        RunnerProviderResult,
        RunnerProviderFailure,
        RunnerOutputLimitExceeded,
        RunnerProcessBoundaryFailure,
        RunnerCancellation,
        RunnerInvocationLost,
    }
    assert AgentProcessCompletion not in members


def test_the_condemned_process_port_reexports_the_runner_evidence_bound() -> None:
    assert (
        MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES
        == MAXIMUM_RUNNER_STANDARD_ERROR_BYTES
    )


def test_runner_provider_result_enforces_the_existing_durable_output_bound() -> None:
    maximum = AgentExecutionResult(b"x" * MAXIMUM_AGENT_OUTPUT_BYTES_V2)

    assert RunnerProviderResult(maximum).result is maximum

    with pytest.raises(ValueError, match="durable output bound"):
        RunnerProviderResult(
            AgentExecutionResult(b"x" * (MAXIMUM_AGENT_OUTPUT_BYTES_V2 + 1))
        )
    with pytest.raises(TypeError, match="decoded agent execution result"):
        RunnerProviderResult(cast(AgentExecutionResult, object()))
    with pytest.raises(TypeError, match="output bytes"):
        RunnerProviderResult(AgentExecutionResult(cast(bytes, "not-bytes")))


@pytest.mark.parametrize(
    "return_code", (-MAXIMUM_SIGNED_INT64 - 1, 0, MAXIMUM_SIGNED_INT64)
)
def test_runner_provider_failure_accepts_every_signed_int64_return_code(
    return_code: int,
) -> None:
    signature = ProcessExitSignature(
        return_code, b"x" * MAXIMUM_RUNNER_STANDARD_ERROR_BYTES
    )

    assert RunnerProviderFailure(signature).exit_signature is signature


@pytest.mark.parametrize(
    "return_code", (-MAXIMUM_SIGNED_INT64 - 2, MAXIMUM_SIGNED_INT64 + 1)
)
def test_runner_provider_failure_refuses_return_codes_outside_signed_int64(
    return_code: int,
) -> None:
    with pytest.raises(ValueError, match="signed int64"):
        RunnerProviderFailure(ProcessExitSignature(return_code, b""))


def test_runner_provider_failure_requires_a_typed_exit_signature() -> None:
    with pytest.raises(TypeError, match="process exit signature"):
        RunnerProviderFailure(cast(ProcessExitSignature, object()))


def test_runner_provider_failure_alone_owns_the_full_stderr_evidence_bound() -> None:
    oversized = ProcessExitSignature(
        1, b"x" * (MAXIMUM_RUNNER_STANDARD_ERROR_BYTES + 1)
    )

    with pytest.raises(ValueError, match="standard error evidence"):
        RunnerProviderFailure(oversized)


@pytest.mark.parametrize(
    "streams",
    (
        frozenset({RunnerOutputStream.STANDARD_OUTPUT}),
        frozenset({RunnerOutputStream.STANDARD_ERROR}),
        frozenset(
            {
                RunnerOutputStream.STANDARD_OUTPUT,
                RunnerOutputStream.STANDARD_ERROR,
            }
        ),
    ),
)
def test_output_limit_evidence_names_a_nonempty_closed_stream_set(
    streams: frozenset[RunnerOutputStream],
) -> None:
    evidence = RunnerOutputLimitExceeded(streams)

    assert tuple(field.name for field in fields(evidence)) == ("exceeded_streams",)
    assert evidence.exceeded_streams == streams


@pytest.mark.parametrize(
    "streams",
    (
        frozenset(),
        {RunnerOutputStream.STANDARD_OUTPUT},
        frozenset({"STANDARD_OUTPUT"}),
    ),
)
def test_output_limit_evidence_refuses_empty_unfrozen_or_foreign_stream_sets(
    streams: object,
) -> None:
    with pytest.raises((TypeError, ValueError), match="stream"):
        RunnerOutputLimitExceeded(cast(frozenset[RunnerOutputStream], streams))


@pytest.mark.parametrize(
    "observation",
    (
        RunnerCancellationObservation.NEVER_LAUNCHED,
        RunnerCancellationObservation.EXITED_BEFORE_SIGNAL,
        RunnerCancellationObservation.REAPED_AFTER_TERM,
        RunnerCancellationObservation.REAPED_AFTER_KILL,
    ),
)
def test_runner_cancellation_accepts_only_physical_observations(
    observation: RunnerCancellationObservation,
) -> None:
    evidence = RunnerCancellation("cancel-17", observation)

    assert evidence.command_id == "cancel-17"
    assert evidence.observation is observation


def test_runner_cancellation_observation_is_the_runner_owned_closed_set() -> None:
    assert set(RunnerCancellationObservation) == {
        RunnerCancellationObservation.NEVER_LAUNCHED,
        RunnerCancellationObservation.EXITED_BEFORE_SIGNAL,
        RunnerCancellationObservation.REAPED_AFTER_TERM,
        RunnerCancellationObservation.REAPED_AFTER_KILL,
    }


@pytest.mark.parametrize(
    "core_observation",
    (
        AgentAttemptCancellationDisposition.NEVER_LAUNCHED,
        AgentAttemptCancellationDisposition.EXITED_BEFORE_SIGNAL,
        AgentAttemptCancellationDisposition.REAPED_AFTER_TERM,
        AgentAttemptCancellationDisposition.REAPED_AFTER_KILL,
    ),
)
def test_runner_cancellation_refuses_equal_valued_core_observations(
    core_observation: AgentAttemptCancellationDisposition,
) -> None:
    assert core_observation.value in {
        observation.value for observation in RunnerCancellationObservation
    }

    with pytest.raises(TypeError, match="typed physical observation"):
        RunnerCancellation(
            "cancel-17",
            cast(RunnerCancellationObservation, core_observation),
        )


def test_runner_cancellation_refuses_untyped_observations() -> None:
    with pytest.raises(TypeError, match="typed physical observation"):
        RunnerCancellation(
            "cancel-17",
            cast(RunnerCancellationObservation, "NEVER_LAUNCHED"),
        )


def test_runner_cancellation_command_ends_at_the_existing_agent_field_bound() -> None:
    RunnerCancellation(
        "x" * MAXIMUM_AGENT_FIELD_CHARACTERS,
        RunnerCancellationObservation.NEVER_LAUNCHED,
    )

    with pytest.raises(ValueError, match=str(MAXIMUM_AGENT_FIELD_CHARACTERS)):
        RunnerCancellation(
            "x" * (MAXIMUM_AGENT_FIELD_CHARACTERS + 1),
            RunnerCancellationObservation.NEVER_LAUNCHED,
        )
    with pytest.raises(ValueError, match=str(MAXIMUM_AGENT_FIELD_CHARACTERS)):
        RunnerCancellation(
            "",
            RunnerCancellationObservation.NEVER_LAUNCHED,
        )


def test_process_boundary_failure_and_invocation_loss_carry_no_payload() -> None:
    assert fields(RunnerProcessBoundaryFailure) == ()
    assert fields(RunnerInvocationLost) == ()
    assert RunnerProcessBoundaryFailure() == RunnerProcessBoundaryFailure()
    assert RunnerInvocationLost() == RunnerInvocationLost()
