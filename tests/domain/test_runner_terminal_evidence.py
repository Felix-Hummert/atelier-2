from __future__ import annotations

from dataclasses import fields, replace
from typing import cast, get_args

import pytest

from atelier2.contracts.agent_attempts import (
    MAXIMUM_RUNNER_STANDARD_ERROR_BYTES,
    AgentAttemptCancellationDisposition,
    AgentAttemptFailureCode,
    AgentAttemptId,
    ProcessExitSignature,
    RunnerCancellation,
    RunnerCancellationObservation,
    RunnerGenerationBinding,
    RunnerGenerationId,
    RunnerInvocationId,
    RunnerInvocationLost,
    RunnerManifestId,
    RunnerOutputLimitExceeded,
    RunnerOutputStream,
    RunnerProcessBoundaryFailure,
    RunnerProviderFailure,
    RunnerProviderResult,
    RunnerTerminalEvidence,
    RunnerTerminalEvidenceAckTombstone,
    RunnerTerminalEvidenceEnvelope,
    RunnerTerminalEvidenceHash,
    RunnerTerminalEvidenceReadback,
)
from atelier2.contracts.agent_transcripts import AssistantTurn, AttemptTranscript
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_FIELD_CHARACTERS,
    MAXIMUM_AGENT_OUTPUT_BYTES_V2,
    MAXIMUM_SIGNED_INT64,
    AgentExecutionRequestHash,
    AgentExecutionResult,
)
from atelier2.ports.agent_attempts import RunnerTerminalEvidenceSource
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


def _ack_tombstone() -> RunnerTerminalEvidenceAckTombstone:
    binding = RunnerGenerationBinding(
        AgentAttemptId("a" * 64),
        AgentExecutionRequestHash("b" * 64),
        RunnerGenerationId("generation-1"),
        RunnerManifestId("c" * 64),
    )
    evidence_hash = RunnerTerminalEvidenceHash("d" * 64)
    return RunnerTerminalEvidenceAckTombstone(binding, None, evidence_hash)


def test_runner_ack_tombstone_keeps_only_the_exact_ack_identity() -> None:
    tombstone = _ack_tombstone()

    assert tuple(field.name for field in fields(tombstone)) == (
        "binding",
        "invocation_id",
        "evidence_hash",
    )
    assert set(get_args(RunnerTerminalEvidenceReadback.__value__)) == {
        RunnerTerminalEvidenceEnvelope,
        RunnerTerminalEvidenceAckTombstone,
    }
    assert {
        name for name in vars(RunnerTerminalEvidenceSource) if not name.startswith("_")
    } == {"readback", "acknowledge"}


@pytest.mark.parametrize(
    ("foreign_field", "message"),
    (
        ("binding", "typed binding"),
        ("invocation_id", "typed invocation id"),
        ("evidence_hash", "typed evidence hash"),
    ),
    ids=("foreign-binding", "foreign-non-none-invocation", "foreign-evidence-hash"),
)
def test_runner_ack_tombstone_refuses_each_foreign_runtime_type(
    foreign_field: str, message: str
) -> None:
    with pytest.raises(TypeError, match=message):
        replace(_ack_tombstone(), **{foreign_field: object()})


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


def test_runner_provider_result_preserves_a_transcript() -> None:
    transcript = AttemptTranscript.of([AssistantTurn("I read the file.")])
    result = AgentExecutionResult(b"answer", transcript)

    assert RunnerProviderResult(result).result is result


def test_runner_provider_result_refuses_an_untyped_transcript() -> None:
    with pytest.raises(TypeError, match="typed transcript"):
        RunnerProviderResult(
            AgentExecutionResult(b"answer", cast(AttemptTranscript, object()))
        )


@pytest.mark.parametrize(
    "failure_code",
    (
        AgentAttemptFailureCode.AGENT_REFUSED,
        AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY,
    ),
)
def test_runner_provider_failure_preserves_each_admitted_failure_form(
    failure_code: AgentAttemptFailureCode,
) -> None:
    transcript = AttemptTranscript.of([AssistantTurn("The provider refused.")])
    signature = ProcessExitSignature(17, b"provider stderr")

    failure = RunnerProviderFailure(signature, failure_code, transcript)

    assert failure.failure_code is failure_code
    assert failure.exit_signature is signature
    assert failure.transcript is transcript


@pytest.mark.parametrize(
    "failure_code",
    tuple(
        code
        for code in AgentAttemptFailureCode
        if code
        not in {
            AgentAttemptFailureCode.AGENT_REFUSED,
            AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY,
        }
    ),
)
def test_runner_provider_failure_refuses_every_other_failure_code(
    failure_code: AgentAttemptFailureCode,
) -> None:
    with pytest.raises(ValueError, match="admitted failure code"):
        RunnerProviderFailure(ProcessExitSignature(0, b""), failure_code)


def test_runner_provider_failure_refuses_an_untyped_failure_code() -> None:
    with pytest.raises(TypeError, match="typed failure code"):
        RunnerProviderFailure(
            ProcessExitSignature(0, b""),
            cast(AgentAttemptFailureCode, "AGENT_REFUSED"),
        )


def test_runner_provider_failure_refuses_an_untyped_transcript() -> None:
    with pytest.raises(TypeError, match="typed transcript"):
        RunnerProviderFailure(
            ProcessExitSignature(0, b""),
            transcript=cast(AttemptTranscript, object()),
        )


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


def test_semantic_evidence_hash_owns_the_envelope_and_all_six_variant_payloads() -> (
    None
):
    binding = RunnerGenerationBinding(
        AgentAttemptId("a" * 64),
        AgentExecutionRequestHash("b" * 64),
        RunnerGenerationId("generation-1"),
        RunnerManifestId("c" * 64),
    )
    invocation = RunnerInvocationId("invocation-1")
    variants = (
        RunnerProviderResult(AgentExecutionResult(b"answer")),
        RunnerProviderFailure(ProcessExitSignature(-9, b"stderr")),
        RunnerOutputLimitExceeded(
            frozenset(
                {
                    RunnerOutputStream.STANDARD_OUTPUT,
                    RunnerOutputStream.STANDARD_ERROR,
                }
            )
        ),
        RunnerProcessBoundaryFailure(),
        RunnerCancellation("cancel-1", RunnerCancellationObservation.REAPED_AFTER_TERM),
        RunnerInvocationLost(),
    )

    hashes = {
        RunnerTerminalEvidenceHash.for_envelope(
            RunnerTerminalEvidenceEnvelope(binding, invocation, evidence)
        )
        for evidence in variants
    }

    assert len(hashes) == 6

    def envelope(
        evidence: RunnerTerminalEvidence,
        *,
        bound: RunnerGenerationBinding = binding,
        invoked: RunnerInvocationId = invocation,
    ) -> RunnerTerminalEvidenceEnvelope:
        return RunnerTerminalEvidenceEnvelope(bound, invoked, evidence)

    provider_result = RunnerProviderResult(AgentExecutionResult(b"answer"))
    provider_failure = RunnerProviderFailure(ProcessExitSignature(-9, b"stderr"))
    first_transcript = AttemptTranscript.of([AssistantTurn("read first")])
    second_transcript = AttemptTranscript.of([AssistantTurn("read second")])
    perturbations = (
        (
            "attempt id",
            envelope(provider_result),
            envelope(
                provider_result,
                bound=RunnerGenerationBinding(
                    AgentAttemptId("d" * 64),
                    binding.request_hash,
                    binding.generation_id,
                    binding.manifest_id,
                ),
            ),
        ),
        (
            "request hash",
            envelope(provider_result),
            envelope(
                provider_result,
                bound=RunnerGenerationBinding(
                    binding.attempt_id,
                    AgentExecutionRequestHash("e" * 64),
                    binding.generation_id,
                    binding.manifest_id,
                ),
            ),
        ),
        (
            "generation id",
            envelope(provider_result),
            envelope(
                provider_result,
                bound=RunnerGenerationBinding(
                    binding.attempt_id,
                    binding.request_hash,
                    RunnerGenerationId("generation-2"),
                    binding.manifest_id,
                ),
            ),
        ),
        (
            "manifest id",
            envelope(provider_result),
            envelope(
                provider_result,
                bound=RunnerGenerationBinding(
                    binding.attempt_id,
                    binding.request_hash,
                    binding.generation_id,
                    RunnerManifestId("f" * 64),
                ),
            ),
        ),
        (
            "invocation id",
            envelope(provider_result),
            envelope(provider_result, invoked=RunnerInvocationId("invocation-2")),
        ),
        (
            "provider output bytes",
            envelope(provider_result),
            envelope(RunnerProviderResult(AgentExecutionResult(b"other answer"))),
        ),
        (
            "provider exit-signature return code",
            envelope(provider_failure),
            envelope(RunnerProviderFailure(ProcessExitSignature(-8, b"stderr"))),
        ),
        (
            "provider failure code",
            envelope(
                RunnerProviderFailure(
                    ProcessExitSignature(-9, b"stderr"),
                    AgentAttemptFailureCode.AGENT_REFUSED,
                    first_transcript,
                )
            ),
            envelope(
                RunnerProviderFailure(
                    ProcessExitSignature(-9, b"stderr"),
                    AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY,
                    first_transcript,
                )
            ),
        ),
        (
            "transcript presence",
            envelope(provider_failure),
            envelope(
                RunnerProviderFailure(
                    provider_failure.exit_signature,
                    provider_failure.failure_code,
                    first_transcript,
                )
            ),
        ),
        (
            "transcript content",
            envelope(
                RunnerProviderFailure(
                    provider_failure.exit_signature,
                    provider_failure.failure_code,
                    first_transcript,
                )
            ),
            envelope(
                RunnerProviderFailure(
                    provider_failure.exit_signature,
                    provider_failure.failure_code,
                    second_transcript,
                )
            ),
        ),
        (
            "provider standard error",
            envelope(provider_failure),
            envelope(RunnerProviderFailure(ProcessExitSignature(-9, b"other stderr"))),
        ),
        (
            "exceeded streams",
            envelope(
                RunnerOutputLimitExceeded(
                    frozenset({RunnerOutputStream.STANDARD_OUTPUT})
                )
            ),
            envelope(
                RunnerOutputLimitExceeded(
                    frozenset({RunnerOutputStream.STANDARD_ERROR})
                )
            ),
        ),
        (
            "cancellation command",
            envelope(
                RunnerCancellation(
                    "cancel-1", RunnerCancellationObservation.REAPED_AFTER_TERM
                )
            ),
            envelope(
                RunnerCancellation(
                    "cancel-2", RunnerCancellationObservation.REAPED_AFTER_TERM
                )
            ),
        ),
        (
            "cancellation observation",
            envelope(
                RunnerCancellation(
                    "cancel-1", RunnerCancellationObservation.REAPED_AFTER_TERM
                )
            ),
            envelope(
                RunnerCancellation(
                    "cancel-1", RunnerCancellationObservation.REAPED_AFTER_KILL
                )
            ),
        ),
        (
            "payloadless variant tag",
            envelope(RunnerProcessBoundaryFailure()),
            envelope(RunnerInvocationLost()),
        ),
    )

    for field_name, left, right in perturbations:
        assert RunnerTerminalEvidenceHash.for_envelope(
            left
        ) != RunnerTerminalEvidenceHash.for_envelope(right), field_name
