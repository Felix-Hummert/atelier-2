from __future__ import annotations

import pytest

from atelier2.adapters.free_runner_executor import (
    FreeRunnerAuthorizationResolver,
    refuse_unbound_runner_a_request,
)
from atelier2.application.run_runner_session import (
    CoreRunnerSession,
    RunnerSessionRefusal,
    cancellation_refusal_code,
)
from atelier2.contracts.agent_attempts import (
    AgentAttemptId,
    AgentAttemptReplacement,
    CancelAgentAttemptRequest,
    RunnerBindingConflict,
    RunnerGenerationBinding,
    RunnerGenerationId,
    RunnerInvocationId,
    RunnerManifestId,
    RunnerTerminalEvidenceHash,
)
from atelier2.contracts.agents import (
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutionRequestHash,
    AgentExecutionRequestV2,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
    ResolvedAgentBinding,
)
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.runner_sessions import (
    RUNNER_SESSION_REFUSAL_CODES,
    RunnerSessionFrame,
    RunnerSessionMessage,
)
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationStale,
    AgentAttemptReplacementNotAllowed,
)


def _profile() -> AuthProfileRevision:
    return AuthProfileRevision(
        profile_id="free-profile",
        revision_number=1,
        provider_id=ProviderId("fake-free"),
        auth_mode=AuthMode.API_KEY,
    )


def test_free_runner_auth_reference_is_a_secret_free_function_of_the_bound_revision() -> (
    None
):
    profile = _profile()
    resolver = FreeRunnerAuthorizationResolver()
    reference = resolver.reference_for(profile)

    authorization = resolver.resolve(profile, reference)

    assert authorization.__class__.__name__ == "FreeRunnerAuthorization"
    assert reference.endswith(profile.revision_hash.value)


@pytest.mark.parametrize(
    "reference", ("wrong", "urn:atelier2:fake-free-auth:v1:" + "d" * 64)
)
def test_free_runner_auth_refuses_changed_or_unbound_references(reference: str) -> None:
    with pytest.raises(ValueError, match="auth-profile-unresolvable"):
        FreeRunnerAuthorizationResolver().resolve(_profile(), reference)


class _Core:
    def __init__(self) -> None:
        self.armed = 0
        self.committed = 0
        self.acknowledged = 0
        self.cancelled = 0
        self.hash = RunnerTerminalEvidenceHash("d" * 64)

    def arm(
        self, binding: RunnerGenerationBinding, invocation: RunnerInvocationId
    ) -> None:
        self.armed += 1

    def commit_terminal_record(
        self, binding: RunnerGenerationBinding, record: bytes
    ) -> RunnerTerminalEvidenceHash:
        self.committed += 1
        return self.hash

    def acknowledge(
        self,
        binding: RunnerGenerationBinding,
        evidence_hash: RunnerTerminalEvidenceHash,
        tombstone: bytes,
    ) -> None:
        del tombstone
        self.acknowledged += 1

    def cancel(self) -> CancelAgentAttemptRequest:
        self.cancelled += 1
        return CancelAgentAttemptRequest(
            RunId("runner-candidate/one"),
            AgentAttemptId("a" * 64),
            "runner-candidate-cancel",
            2,
            AgentAttemptReplacement.NONE,
        )


def _binding() -> RunnerGenerationBinding:
    return RunnerGenerationBinding(
        AgentAttemptId("a" * 64),
        AgentExecutionRequestHash("b" * 64),
        RunnerGenerationId("A" * 43),
        RunnerManifestId("c" * 64),
    )


def _frame(
    message: RunnerSessionMessage, sequence: int, payload: tuple[bytes, ...] = ()
) -> RunnerSessionFrame:
    return RunnerSessionFrame(
        message, sequence, _binding(), RunnerInvocationId("B" * 43), payload
    )


def test_core_session_arms_once_then_commits_acknowledges_and_releases_in_order() -> (
    None
):
    core = _Core()
    session = CoreRunnerSession(_binding(), core)

    offer = session.accept(_frame(RunnerSessionMessage.INVOCATION_OFFER, 1))
    assert offer is not None
    assert offer.message is RunnerSessionMessage.PREPARE
    launch = session.accept(_frame(RunnerSessionMessage.READY, 2, (b"x",) * 11))
    assert launch is not None
    assert launch.message is RunnerSessionMessage.LAUNCH
    assert (
        session.accept(_frame(RunnerSessionMessage.STARTED, 3, (b"\x00" * 8,))) is None
    )
    readback = session.accept(
        _frame(RunnerSessionMessage.TERMINAL_AVAILABLE, 4, (b"d" * 64,))
    )
    assert readback is not None
    assert readback.message is RunnerSessionMessage.READBACK
    acknowledgement = session.accept_terminal_record(
        _frame(RunnerSessionMessage.TERMINAL_RECORD, 5, (b"record",))
    )
    assert acknowledgement.message is RunnerSessionMessage.ACK
    release = session.accept(
        _frame(RunnerSessionMessage.ACK_TOMBSTONE, 6, (b"tombstone",))
    )
    assert release is not None
    assert release.message is RunnerSessionMessage.RELEASE
    assert (
        session.accept(_frame(RunnerSessionMessage.RELEASED, 7, (b"d" * 64,))) is None
    )
    assert (core.armed, core.committed, core.acknowledged) == (1, 1, 1)


def test_core_session_refuses_a_launch_before_ready_without_arming() -> None:
    core = _Core()
    session = CoreRunnerSession(_binding(), core)

    with pytest.raises(RunnerSessionRefusal, match="out-of-order"):
        session.accept(_frame(RunnerSessionMessage.LAUNCH, 1))

    assert core.armed == 0


@pytest.mark.proves("runner-cancel-none")
def test_core_session_persists_one_none_replacement_cancel_before_signalling() -> None:
    core = _Core()
    session = CoreRunnerSession(_binding(), core)

    assert session.accept(_frame(RunnerSessionMessage.INVOCATION_OFFER, 1)) is not None
    assert (
        session.accept(_frame(RunnerSessionMessage.READY, 2, (b"x",) * 11)) is not None
    )
    assert (
        session.accept(_frame(RunnerSessionMessage.STARTED, 3, (b"\x00" * 8,))) is None
    )

    cancel = session.cancel()
    duplicate = session.cancel()

    assert cancel is duplicate
    assert cancel.message is RunnerSessionMessage.CANCEL
    assert cancel.sequence == 3
    assert cancel.payload == (
        b"runner-candidate/one",
        b"runner-candidate-cancel",
        (2).to_bytes(8, "big"),
        b"NONE",
    )
    assert core.cancelled == 1


def test_core_session_refuses_cancel_before_runner_started() -> None:
    with pytest.raises(RunnerSessionRefusal, match="runner-cancel-conflict"):
        CoreRunnerSession(_binding(), _Core()).cancel()


def test_core_session_duplicate_offer_is_idempotent_without_a_second_arm() -> None:
    core = _Core()
    session = CoreRunnerSession(_binding(), core)
    offer = _frame(RunnerSessionMessage.INVOCATION_OFFER, 1)

    first = session.accept(offer)
    second = session.accept(offer)

    assert first == second
    assert core.armed == 0


def test_core_session_refuses_replayed_bytes_at_the_same_sequence() -> None:
    session = CoreRunnerSession(_binding(), _Core())
    session.accept(_frame(RunnerSessionMessage.INVOCATION_OFFER, 1))

    with pytest.raises(RunnerSessionRefusal, match="runner-session-replay"):
        session.accept(_frame(RunnerSessionMessage.READY, 1, (b"x",) * 11))


def test_core_session_refuses_a_sequence_gap() -> None:
    session = CoreRunnerSession(_binding(), _Core())

    with pytest.raises(RunnerSessionRefusal, match="runner-session-sequence-mismatch"):
        session.accept(_frame(RunnerSessionMessage.INVOCATION_OFFER, 2))


def test_core_session_refuses_a_binding_mismatch() -> None:
    session = CoreRunnerSession(_binding(), _Core())
    other = RunnerSessionFrame(
        RunnerSessionMessage.INVOCATION_OFFER,
        1,
        RunnerGenerationBinding(
            AgentAttemptId("e" * 64),
            AgentExecutionRequestHash("f" * 64),
            RunnerGenerationId("C" * 43),
            RunnerManifestId("d" * 64),
        ),
        RunnerInvocationId("B" * 43),
        (),
    )

    with pytest.raises(RunnerSessionRefusal, match="runner-session-binding-mismatch"):
        session.accept(other)


def test_core_session_maps_arm_conflict_without_launch() -> None:
    class _ArmConflict(_Core):
        def arm(
            self, binding: RunnerGenerationBinding, invocation: RunnerInvocationId
        ) -> None:
            raise RunnerBindingConflict("runner invocation retry differs")

    core = _ArmConflict()
    session = CoreRunnerSession(_binding(), core)
    session.accept(_frame(RunnerSessionMessage.INVOCATION_OFFER, 1))

    with pytest.raises(RunnerSessionRefusal, match="runner-arm-conflict"):
        session.accept(_frame(RunnerSessionMessage.READY, 2, (b"x",) * 11))
    assert core.armed == 0


def test_core_session_refuses_cancel_after_terminal_evidence() -> None:
    session = CoreRunnerSession(_binding(), _Core())
    session.accept(_frame(RunnerSessionMessage.INVOCATION_OFFER, 1))
    session.accept(_frame(RunnerSessionMessage.READY, 2, (b"x",) * 11))
    session.accept(_frame(RunnerSessionMessage.STARTED, 3, (b"\x00" * 8,)))
    session.accept(_frame(RunnerSessionMessage.TERMINAL_AVAILABLE, 4, (b"d" * 64,)))

    with pytest.raises(RunnerSessionRefusal, match="runner-cancel-conflict"):
        session.cancel()


class _ReplacementOneCore(_Core):
    def cancel(self) -> CancelAgentAttemptRequest:
        self.cancelled += 1
        return CancelAgentAttemptRequest(
            RunId("runner-candidate/one"),
            AgentAttemptId("a" * 64),
            "runner-candidate-cancel",
            2,
            AgentAttemptReplacement.ONE,
        )


def test_core_session_refuses_replacement_one_without_a_cancel_frame() -> None:
    core = _ReplacementOneCore()
    session = CoreRunnerSession(_binding(), core)
    session.accept(_frame(RunnerSessionMessage.INVOCATION_OFFER, 1))
    session.accept(_frame(RunnerSessionMessage.READY, 2, (b"x",) * 11))
    session.accept(_frame(RunnerSessionMessage.STARTED, 3, (b"\x00" * 8,)))

    with pytest.raises(
        RunnerSessionRefusal, match="runner-replacement-not-supported-a"
    ):
        session.cancel()
    with pytest.raises(
        RunnerSessionRefusal, match="runner-replacement-not-supported-a"
    ):
        session.cancel()
    assert core.cancelled == 2


@pytest.mark.proves("runner-replacement-not-supported-a")
def test_replacement_not_allowed_maps_to_the_a_refusal_without_a_second_attempt() -> (
    None
):
    assert (
        cancellation_refusal_code(AgentAttemptReplacementNotAllowed())
        == "runner-replacement-not-supported-a"
    )
    assert (
        cancellation_refusal_code(AgentAttemptCancellationStale())
        == "runner-cancel-conflict"
    )


def test_closed_refusal_vocabulary_is_the_reviewed_a_set() -> None:
    assert "runner-replacement-not-supported-a" in RUNNER_SESSION_REFUSAL_CODES
    assert "runner-cancel-conflict" in RUNNER_SESSION_REFUSAL_CODES
    assert len(RUNNER_SESSION_REFUSAL_CODES) == 32


def _free_request(**changes: object) -> AgentExecutionRequestV2:
    auth = AuthProfileRevision(
        "candidate", 1, ProviderId("fake-free"), AuthMode.API_KEY
    )
    configuration = AgentConfigurationRevision(
        "free",
        auth.revision_hash,
        AgentExecutorRevision("fake-free/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    run_id = RunId("runner-candidate/one")
    workflow = WorkflowRevisionHash("a" * 64)
    node_id = "execute"
    round_ordinal = 1
    request = AgentExecutionRequestV2(
        NodeExecutionId.for_node(run_id, workflow, node_id, round_ordinal),
        run_id,
        workflow,
        node_id,
        ResolvedAgentBinding(AgentRole("runner"), configuration, auth),
        AgentExecutorOperationalIdentity("free-runner-candidate"),
        b"Return the one candidate result.",
    )
    for name, value in changes.items():
        object.__setattr__(request, name, value)
        if name == "round_ordinal":
            if type(value) is not int:
                raise TypeError("round ordinal must be an int")
            object.__setattr__(
                request,
                "node_execution_id",
                NodeExecutionId.for_node(run_id, workflow, node_id, value),
            )
    return request


def test_a_request_subset_accepts_the_schema_free_fake_free_candidate() -> None:
    refuse_unbound_runner_a_request(_free_request())


def test_a_request_subset_refuses_unbound_schema_and_turn_limit() -> None:
    with pytest.raises(ValueError, match="runner-a-output-schema-unbound"):
        refuse_unbound_runner_a_request(
            _free_request(declared_output_schema_bytes=b"true")
        )
    with pytest.raises(ValueError, match="runner-a-turn-limit-unbound"):
        refuse_unbound_runner_a_request(_free_request(maximum_assistant_turns=1))


def test_a_request_subset_refuses_round_outside_the_uint64_transport() -> None:
    with pytest.raises(ValueError, match="runner-a-round-out-of-range"):
        refuse_unbound_runner_a_request(_free_request(round_ordinal=2**64))


def test_a_request_subset_refuses_oversized_run_id_text() -> None:
    run_id = RunId("r" * 4097)
    workflow = WorkflowRevisionHash("a" * 64)
    request = _free_request()
    object.__setattr__(request, "run_id", run_id)
    object.__setattr__(
        request,
        "node_execution_id",
        NodeExecutionId.for_node(run_id, workflow, "execute", 1),
    )
    with pytest.raises(ValueError, match="runner-a-text-oversized"):
        refuse_unbound_runner_a_request(request)


def test_a_request_subset_refuses_a_different_hash_bound_executor() -> None:
    with pytest.raises(ValueError, match="runner-a-executor-unavailable"):
        refuse_unbound_runner_a_request(
            _free_request(
                executor_operational_identity=AgentExecutorOperationalIdentity(
                    "not-the-candidate"
                )
            )
        )
