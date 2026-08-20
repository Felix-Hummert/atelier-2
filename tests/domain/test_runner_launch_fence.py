from __future__ import annotations

from dataclasses import dataclass, fields, replace
from typing import cast

import pytest

from atelier2.contracts.agent_attempts import (
    AgentAttemptId,
    AgentAttemptState,
    ProcessExitSignature,
    RunnerBindingConflict,
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
    RunnerTerminalEvidenceEnvelope,
)
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_FIELD_CHARACTERS,
    AgentExecutionRequestHash,
    AgentExecutionResult,
)
from atelier2.contracts.run_projections import (
    PublicAgentAttemptState,
    public_agent_attempt_state,
)


@dataclass
class _AcceptedGeneration:
    binding: RunnerGenerationBinding
    invocation_id: RunnerInvocationId
    launched: bool = False
    terminal_evidence: RunnerTerminalEvidenceEnvelope | None = None


class _FakeRunner:
    """Operation-shaped proof double; no production Runner boundary exists in A.2."""

    def __init__(self) -> None:
        self._accepted: dict[RunnerGenerationId, _AcceptedGeneration] = {}
        self.provider_start_count = 0

    def accept(self, binding: RunnerGenerationBinding) -> RunnerInvocationId:
        accepted = self._accepted.get(binding.generation_id)
        if accepted is not None:
            if accepted.binding != binding:
                raise RunnerBindingConflict(
                    "runner generation is already bound to different work"
                )
            return accepted.invocation_id

        invocation_id = RunnerInvocationId(
            f"runner-invocation-{len(self._accepted) + 1}"
        )
        self._accepted[binding.generation_id] = _AcceptedGeneration(
            binding, invocation_id
        )
        return invocation_id

    def launch(
        self,
        binding: RunnerGenerationBinding,
        invocation_id: RunnerInvocationId,
    ) -> None:
        accepted = self._accepted.get(binding.generation_id)
        if (
            accepted is None
            or accepted.binding != binding
            or accepted.invocation_id != invocation_id
        ):
            raise RunnerBindingConflict(
                "runner launch differs from its accepted generation binding"
            )
        if not accepted.launched:
            accepted.launched = True
            self.provider_start_count += 1

    def observe(
        self, envelope: RunnerTerminalEvidenceEnvelope
    ) -> RunnerTerminalEvidenceEnvelope:
        accepted = self._accepted.get(envelope.binding.generation_id)
        if accepted is None or accepted.binding != envelope.binding:
            raise RunnerBindingConflict(
                "runner evidence differs from its accepted generation binding"
            )
        if (
            envelope.invocation_id is not None
            and envelope.invocation_id != accepted.invocation_id
        ):
            raise RunnerBindingConflict(
                "runner evidence differs from its accepted invocation"
            )
        if accepted.terminal_evidence is None:
            accepted.terminal_evidence = envelope
        return accepted.terminal_evidence

    def readback(
        self, generation_id: RunnerGenerationId
    ) -> RunnerTerminalEvidenceEnvelope | None:
        accepted = self._accepted[generation_id]
        return accepted.terminal_evidence

    @property
    def accepted_generation_ids(self) -> frozenset[RunnerGenerationId]:
        return frozenset(self._accepted)


class _FakeCoreFence:
    """The Core side of the A.2 scenario, deliberately local to this test."""

    def __init__(self, runner: _FakeRunner) -> None:
        self._runner = runner
        self._binding: RunnerGenerationBinding | None = None
        self._invocation_id: RunnerInvocationId | None = None
        self.attempt_state = AgentAttemptState.PREPARED

    def place(
        self,
        binding: RunnerGenerationBinding,
        no_launch_evidence: RunnerTerminalEvidenceEnvelope | None = None,
    ) -> RunnerInvocationId:
        if self._binding is not None:
            replacement_allowed = (
                self.attempt_state is AgentAttemptState.PREPARED
                and no_launch_evidence is not None
                and no_launch_evidence.binding == self._binding
                and isinstance(no_launch_evidence.evidence, RunnerCancellation)
                and no_launch_evidence.evidence.observation
                is RunnerCancellationObservation.NEVER_LAUNCHED
            )
            if not replacement_allowed:
                raise RunnerBindingConflict(
                    "a fresh generation requires authoritative pre-arm no-launch evidence"
                )
        self._binding = binding
        self._invocation_id = self._runner.accept(binding)
        return self._invocation_id

    def arm_and_launch(self) -> None:
        assert self._binding is not None
        assert self._invocation_id is not None
        self.attempt_state = AgentAttemptState.LAUNCH_ARMED
        self._runner.launch(self._binding, self._invocation_id)

    @property
    def binding(self) -> RunnerGenerationBinding | None:
        return self._binding


def _binding(
    *,
    attempt_fill: str = "a",
    request_fill: str = "b",
    generation: str = "core-generation-1",
    manifest_bytes: bytes = b"runner-offer-v1",
) -> RunnerGenerationBinding:
    return RunnerGenerationBinding(
        attempt_id=AgentAttemptId(attempt_fill * 64),
        request_hash=AgentExecutionRequestHash(request_fill * 64),
        generation_id=RunnerGenerationId(generation),
        manifest_id=RunnerManifestId.of(manifest_bytes),
    )


def _six_terminal_evidence_variants() -> tuple[RunnerTerminalEvidence, ...]:
    return (
        RunnerProviderResult(AgentExecutionResult(b"paid-result")),
        RunnerProviderFailure(ProcessExitSignature(17, b"provider stopped")),
        RunnerOutputLimitExceeded(frozenset((RunnerOutputStream.STANDARD_OUTPUT,))),
        RunnerProcessBoundaryFailure(),
        RunnerCancellation(
            "cancel-1", RunnerCancellationObservation.EXITED_BEFORE_SIGNAL
        ),
        RunnerInvocationLost(),
    )


def _evidence_requiring_invocation() -> tuple[RunnerTerminalEvidence, ...]:
    provider_result, provider_failure, output_limit, boundary_failure, _, lost = (
        _six_terminal_evidence_variants()
    )
    return (
        provider_result,
        provider_failure,
        output_limit,
        boundary_failure,
        RunnerCancellation(
            "cancel-1", RunnerCancellationObservation.EXITED_BEFORE_SIGNAL
        ),
        RunnerCancellation("cancel-1", RunnerCancellationObservation.REAPED_AFTER_TERM),
        RunnerCancellation("cancel-1", RunnerCancellationObservation.REAPED_AFTER_KILL),
        lost,
    )


def test_runner_manifest_id_is_the_sha256_identity_of_the_exact_offer() -> None:
    first = RunnerManifestId.of(b"runner-offer-v1")
    duplicate = RunnerManifestId.of(b"runner-offer-v1")
    changed = RunnerManifestId.of(b"runner-offer-v2")

    assert first == duplicate
    assert first != changed
    assert first.value == (
        "80937033b72dad4d1d6c720e5537ebd179469a7f0e81ea0ee2cc93f3735d3bc6"
    )


@pytest.mark.parametrize("identity_type", (RunnerGenerationId, RunnerInvocationId))
def test_runner_opaque_identities_share_the_existing_agent_field_bound(
    identity_type: type[RunnerGenerationId | RunnerInvocationId],
) -> None:
    identity_type("x" * MAXIMUM_AGENT_FIELD_CHARACTERS)

    for invalid in (
        "",
        "x" * (MAXIMUM_AGENT_FIELD_CHARACTERS + 1),
        cast(str, object()),
    ):
        with pytest.raises(ValueError, match=str(MAXIMUM_AGENT_FIELD_CHARACTERS)):
            identity_type(invalid)


@pytest.mark.parametrize(
    ("field_name", "foreign_value"),
    (
        ("attempt_id", object()),
        ("request_hash", object()),
        ("generation_id", object()),
        ("manifest_id", object()),
    ),
)
def test_generation_binding_requires_each_owner_typed(
    field_name: str, foreign_value: object
) -> None:
    with pytest.raises(TypeError, match=field_name.replace("_", " ")):
        replace(_binding(), **{field_name: foreign_value})


def test_generation_binding_contains_only_the_four_decided_identities() -> None:
    binding = _binding()

    assert tuple(field.name for field in fields(binding)) == (
        "attempt_id",
        "request_hash",
        "generation_id",
        "manifest_id",
    )
    assert RunnerGenerationId("same") != RunnerInvocationId("same")


def test_exact_generation_delivery_returns_one_invocation_and_starts_once() -> None:
    runner = _FakeRunner()
    binding = _binding()

    first = runner.accept(binding)
    second = runner.accept(binding)
    runner.launch(binding, first)
    runner.launch(binding, second)

    assert first == second
    assert runner.provider_start_count == 1


@pytest.mark.parametrize(
    "changed_binding",
    (
        _binding(attempt_fill="c"),
        _binding(request_fill="d"),
        _binding(manifest_bytes=b"runner-offer-v2"),
    ),
    ids=("attempt", "request", "manifest"),
)
def test_changed_binding_under_one_generation_conflicts_before_start(
    changed_binding: RunnerGenerationBinding,
) -> None:
    runner = _FakeRunner()
    runner.accept(_binding())

    with pytest.raises(RunnerBindingConflict, match="different work"):
        runner.accept(changed_binding)

    assert runner.provider_start_count == 0


def test_launch_refuses_a_second_invocation_for_one_generation() -> None:
    runner = _FakeRunner()
    binding = _binding()
    runner.accept(binding)

    with pytest.raises(RunnerBindingConflict, match="accepted generation"):
        runner.launch(binding, RunnerInvocationId("runner-invocation-other"))

    assert runner.provider_start_count == 0


@pytest.mark.parametrize("drift", ("binding", "invocation"))
def test_evidence_delivery_refuses_identity_drift_without_storing_it(
    drift: str,
) -> None:
    runner = _FakeRunner()
    binding = _binding()
    invocation_id = runner.accept(binding)
    runner.launch(binding, invocation_id)
    envelope = RunnerTerminalEvidenceEnvelope(
        binding=(
            replace(binding, request_hash=AgentExecutionRequestHash("d" * 64))
            if drift == "binding"
            else binding
        ),
        invocation_id=(
            RunnerInvocationId("runner-invocation-other")
            if drift == "invocation"
            else invocation_id
        ),
        evidence=RunnerProviderResult(AgentExecutionResult(b"paid-result")),
    )

    with pytest.raises(RunnerBindingConflict, match=drift):
        runner.observe(envelope)

    assert runner.readback(binding.generation_id) is None
    assert runner.accepted_generation_ids == frozenset((binding.generation_id,))
    assert runner.provider_start_count == 1
    assert runner.accept(binding) is invocation_id


@pytest.mark.parametrize("bind_accepted_invocation", (False, True))
def test_never_launched_accepts_none_or_the_exact_accepted_invocation(
    bind_accepted_invocation: bool,
) -> None:
    runner = _FakeRunner()
    binding = _binding()
    accepted_invocation = runner.accept(binding)
    authoritative_no_launch = RunnerTerminalEvidenceEnvelope(
        binding=binding,
        invocation_id=accepted_invocation if bind_accepted_invocation else None,
        evidence=RunnerCancellation(
            "cancel-1", RunnerCancellationObservation.NEVER_LAUNCHED
        ),
    )

    assert authoritative_no_launch.invocation_id is (
        accepted_invocation if bind_accepted_invocation else None
    )
    assert runner.observe(authoritative_no_launch) is authoritative_no_launch
    assert runner.accept(binding) is accepted_invocation
    assert tuple(field.name for field in fields(authoritative_no_launch)) == (
        "binding",
        "invocation_id",
        "evidence",
    )


@pytest.mark.parametrize(
    "evidence",
    _evidence_requiring_invocation(),
    ids=(
        "provider-result",
        "provider-failure",
        "output-limit",
        "process-boundary",
        "cancel-exited-before-signal",
        "cancel-reaped-after-term",
        "cancel-reaped-after-kill",
        "invocation-lost",
    ),
)
def test_every_non_never_launched_evidence_refuses_a_missing_invocation(
    evidence: RunnerTerminalEvidence,
) -> None:
    with pytest.raises(ValueError, match="invocation"):
        RunnerTerminalEvidenceEnvelope(
            binding=_binding(),
            invocation_id=None,
            evidence=evidence,
        )


def test_evidence_identity_requires_typed_binding_and_invocation() -> None:
    envelope = RunnerTerminalEvidenceEnvelope(
        binding=_binding(),
        invocation_id=RunnerInvocationId("runner-invocation-1"),
        evidence=RunnerProviderResult(AgentExecutionResult(b"paid-result")),
    )

    with pytest.raises(TypeError, match="typed binding"):
        replace(envelope, binding=object())
    with pytest.raises(TypeError, match="typed invocation"):
        replace(envelope, invocation_id=object())


@pytest.mark.parametrize(
    "evidence",
    _six_terminal_evidence_variants(),
    ids=(
        "provider-result",
        "provider-failure",
        "output-limit",
        "process-boundary",
        "cancellation",
        "invocation-lost",
    ),
)
def test_each_terminal_evidence_variant_accepts_the_exact_runner_invocation(
    evidence: RunnerTerminalEvidence,
) -> None:
    runner = _FakeRunner()
    binding = _binding()
    invocation_id = runner.accept(binding)
    runner.launch(binding, invocation_id)
    envelope = RunnerTerminalEvidenceEnvelope(
        binding=binding,
        invocation_id=invocation_id,
        evidence=evidence,
    )

    assert envelope.binding is binding
    assert envelope.invocation_id is invocation_id
    assert envelope.evidence is evidence
    assert runner.observe(envelope) is envelope
    assert runner.accept(binding) is invocation_id


def test_terminal_evidence_envelope_refuses_foreign_evidence() -> None:
    envelope = RunnerTerminalEvidenceEnvelope(
        binding=_binding(),
        invocation_id=RunnerInvocationId("runner-invocation-1"),
        evidence=RunnerProviderResult(AgentExecutionResult(b"paid-result")),
    )
    with pytest.raises(TypeError, match="terminal evidence"):
        replace(envelope, evidence=object())


@pytest.mark.parametrize("bind_accepted_invocation", (False, True))
def test_authoritative_pre_arm_no_launch_allows_one_fresh_generation(
    bind_accepted_invocation: bool,
) -> None:
    runner = _FakeRunner()
    core = _FakeCoreFence(runner)
    first = _binding()
    first_invocation = core.place(first)
    no_launch = runner.observe(
        RunnerTerminalEvidenceEnvelope(
            binding=first,
            invocation_id=first_invocation if bind_accepted_invocation else None,
            evidence=RunnerCancellation(
                "cancel-1", RunnerCancellationObservation.NEVER_LAUNCHED
            ),
        )
    )

    second = replace(first, generation_id=RunnerGenerationId("core-generation-2"))
    core.place(second, no_launch)
    core.arm_and_launch()

    assert runner.provider_start_count == 1


@pytest.mark.parametrize(
    "refusal_scenario",
    ("prepared-silence", "prepared-other-evidence", "armed-never-launched"),
)
def test_fresh_generation_refuses_silence_wrong_fact_and_post_arm_no_launch(
    refusal_scenario: str,
) -> None:
    runner = _FakeRunner()
    core = _FakeCoreFence(runner)
    first = _binding()
    first_invocation = core.place(first)
    evidence: RunnerTerminalEvidenceEnvelope | None = None

    if refusal_scenario == "prepared-other-evidence":
        evidence = runner.observe(
            RunnerTerminalEvidenceEnvelope(
                binding=first,
                invocation_id=first_invocation,
                evidence=RunnerCancellation(
                    "cancel-1",
                    RunnerCancellationObservation.EXITED_BEFORE_SIGNAL,
                ),
            )
        )
    elif refusal_scenario == "armed-never-launched":
        core.arm_and_launch()
        evidence = RunnerTerminalEvidenceEnvelope(
            binding=first,
            invocation_id=first_invocation,
            evidence=RunnerCancellation(
                "cancel-1",
                RunnerCancellationObservation.NEVER_LAUNCHED,
            ),
        )

    evidence_before_refusal = runner.readback(first.generation_id)
    starts_before_refusal = runner.provider_start_count
    second = replace(first, generation_id=RunnerGenerationId("core-generation-2"))

    with pytest.raises(RunnerBindingConflict, match="pre-arm no-launch evidence"):
        core.place(second, evidence)

    evidence_after_refusal = runner.readback(first.generation_id)
    assert core.binding is first
    assert runner.accepted_generation_ids == frozenset((first.generation_id,))
    assert runner.provider_start_count == starts_before_refusal
    assert evidence_after_refusal is evidence_before_refusal
    assert evidence_after_refusal is None or not isinstance(
        evidence_after_refusal.evidence, RunnerInvocationLost
    )
    assert runner.accept(first) is first_invocation


def test_silence_at_or_after_arm_stays_possibly_ran_without_replacement() -> None:
    runner = _FakeRunner()
    core = _FakeCoreFence(runner)
    first = _binding()
    first_invocation = core.place(first)
    core.arm_and_launch()

    assert runner.readback(first.generation_id) is None
    assert public_agent_attempt_state(core.attempt_state) is (
        PublicAgentAttemptState.POSSIBLY_RAN
    )
    second = replace(first, generation_id=RunnerGenerationId("core-generation-2"))
    with pytest.raises(RunnerBindingConflict, match="no-launch evidence"):
        core.place(second)

    assert runner.provider_start_count == 1
    assert runner.readback(first.generation_id) is None
    assert runner.accept(first) is first_invocation


def test_late_cancel_does_not_erase_observed_completion_or_change_generation() -> None:
    runner = _FakeRunner()
    binding = _binding()
    invocation_id = runner.accept(binding)
    runner.launch(binding, invocation_id)
    completion = runner.observe(
        RunnerTerminalEvidenceEnvelope(
            binding=binding,
            invocation_id=invocation_id,
            evidence=RunnerProviderResult(AgentExecutionResult(b"paid-result")),
        )
    )

    after_cancel = runner.observe(
        RunnerTerminalEvidenceEnvelope(
            binding=binding,
            invocation_id=invocation_id,
            evidence=RunnerCancellation(
                "cancel-1", RunnerCancellationObservation.EXITED_BEFORE_SIGNAL
            ),
        )
    )

    assert after_cancel is completion
    assert after_cancel.binding.generation_id is binding.generation_id
    assert isinstance(after_cancel.evidence, RunnerProviderResult)
