from __future__ import annotations

import socket
import ssl
import struct
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from atelier2.adapters.claude_subscription import (
    CLAUDE_SUBSCRIPTION_EXECUTOR_KEY,
    CONFORMANT_CLAUDE_VERSIONS,
)
from atelier2.adapters.free_runner_executor import (
    FreeRunnerCandidateExecutor,
    FreeRunnerHoldJob,
    FreeRunnerJobRefused,
    FreeRunnerPrintJob,
    decode_free_runner_job,
    encode_free_runner_job,
    refuse_unbound_runner_a_request,
)
from atelier2.adapters.runner_tls import runner_uri_for_invocation
from atelier2.application.run_runner_session import (
    CoreRunnerSession,
    RunnerSessionRefusal,
    cancellation_refusal_code,
    decode_runner_prepare_payload,
    encode_runner_prepare_payload,
    encode_runner_ready_payload,
    require_matching_evidence_hash,
    require_ready_matches_manifest,
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
from atelier2.contracts.runner_manifests import (
    ABSENT_PROVIDER_CLI,
    MeasuredProviderCli,
    MeasuredProviderCliVersion,
    candidate_runner_manifest,
)
from atelier2.contracts.runner_session_codec import (
    PREPARE_AUTH_REFERENCE_FIELD,
    encode_runner_session_frame,
)
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
from atelier2.runner.__main__ import (
    CandidateScenario,
    _declared_scenario,
    _invocation_for_session,
    _load_verified_client_identity,
)
from atelier2.runner.authorization import (
    AuthReference,
    free_runner_auth_reference,
    resolve_free_runner_authorization,
)
from atelier2.runner.executors import (
    RunnerExecutorUnavailable,
    RunnerToolchainUnpinned,
    runner_executor_cli_pin,
    select_runner_executor,
)
from atelier2.runner.session import _CoreFrameFence


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
    reference = free_runner_auth_reference(profile)

    authorization = resolve_free_runner_authorization(profile, reference)

    assert authorization.__class__.__name__ == "FreeRunnerAuthorization"
    assert reference.value.endswith(profile.revision_hash.value)


@pytest.mark.parametrize(
    "reference", ("wrong", "urn:atelier2:fake-free-auth:v1:" + "d" * 64)
)
def test_free_runner_auth_refuses_changed_or_unbound_references(reference: str) -> None:
    with pytest.raises(ValueError, match="auth-profile-unresolvable"):
        resolve_free_runner_authorization(_profile(), AuthReference(reference))


@pytest.mark.parametrize(
    "document", (FreeRunnerPrintJob("runner candidate"), FreeRunnerHoldJob(5.0))
)
def test_free_runner_job_round_trips_through_its_wire_form(
    document: FreeRunnerPrintJob | FreeRunnerHoldJob,
) -> None:
    assert decode_free_runner_job(encode_free_runner_job(document)) == document


@pytest.mark.parametrize(
    "data",
    (
        b"not-json",
        b"[]",
        b'{"kind": "surprise"}',
        b'{"kind": "print"}',
        b'{"kind": "print", "text": 1}',
        b'{"kind": "print", "text": ""}',
        b'{"kind": "hold"}',
        b'{"kind": "hold", "hold_seconds": "five"}',
        b'{"kind": "hold", "hold_seconds": true}',
        b'{"kind": "hold", "hold_seconds": -1}',
    ),
    ids=(
        "not-json",
        "not-an-object",
        "unknown-kind",
        "print-missing-text",
        "print-text-not-a-string",
        "print-text-empty",
        "hold-missing-seconds",
        "hold-seconds-not-a-number",
        "hold-seconds-a-bool",
        "hold-seconds-not-positive",
    ),
)
def test_free_runner_job_refuses_every_document_but_its_two(data: bytes) -> None:
    with pytest.raises(FreeRunnerJobRefused, match="free-runner-job-refused"):
        decode_free_runner_job(data)


def test_select_runner_executor_returns_the_fake_free_candidate_executor() -> None:
    executor = select_runner_executor(_candidate_manifest())

    assert isinstance(executor, FreeRunnerCandidateExecutor)


@pytest.mark.parametrize(
    ("provider_id", "executor_revision"),
    (("unknown-provider", "fake-free/v1"), ("fake-free", "fake-free/v2")),
    ids=("unknown-provider", "unknown-revision"),
)
def test_select_runner_executor_refuses_an_unknown_provider_and_revision_combination(
    provider_id: str, executor_revision: str
) -> None:
    manifest = candidate_runner_manifest(
        source_commit="a" * 40,
        image_digest="sha256:" + "b" * 64,
        required_landlock_abi=1,
        executor_revision=executor_revision,
        executor_operational_identity="free-runner-candidate",
        provider_id=provider_id,
        auth_mode="api_key",
        requested_capability="headless",
    )

    with pytest.raises(RunnerExecutorUnavailable, match="runner-executor-unavailable"):
        select_runner_executor(manifest)


_INVOCATION = RunnerInvocationId("B" * 43)


class _Core:
    def __init__(self) -> None:
        self.armed = 0
        self.armed_invocation: RunnerInvocationId | None = None
        self.committed = 0
        self.acknowledged = 0
        self.cancelled = 0
        self.hash = RunnerTerminalEvidenceHash("d" * 64)

    def arm(
        self, binding: RunnerGenerationBinding, invocation: RunnerInvocationId
    ) -> None:
        self.armed_invocation = invocation
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
    return RunnerSessionFrame(message, sequence, _binding(), _INVOCATION, payload)


def _candidate_manifest():
    return candidate_runner_manifest(
        source_commit="a" * 40,
        image_digest="sha256:" + "b" * 64,
        required_landlock_abi=1,
        executor_revision="fake-free/v1",
        executor_operational_identity="free-runner-candidate",
        provider_id="fake-free",
        auth_mode="api_key",
        requested_capability="headless",
    )


def _session(core: _Core | None = None) -> CoreRunnerSession:
    request = _free_request()
    reference = free_runner_auth_reference(request.resolved_binding.auth_profile).value
    return CoreRunnerSession(
        _binding(),
        core if core is not None else _Core(),
        encode_runner_prepare_payload(request, reference),
        _candidate_manifest(),
        reference,
        _INVOCATION,
        runner_executor_cli_pin(_candidate_manifest()),
    )


def _ready_payload(
    measured_cli: MeasuredProviderCliVersion = ABSENT_PROVIDER_CLI,
) -> tuple[bytes, ...]:
    request = _free_request()
    reference = free_runner_auth_reference(request.resolved_binding.auth_profile).value
    return encode_runner_ready_payload(_candidate_manifest(), reference, measured_cli)


def test_core_session_arms_once_then_commits_acknowledges_and_releases_in_order() -> (
    None
):
    core = _Core()
    session = _session(core)

    offer = session.accept(_frame(RunnerSessionMessage.INVOCATION_OFFER, 1))
    assert offer is not None
    assert offer.message is RunnerSessionMessage.PREPARE
    launch = session.accept(_frame(RunnerSessionMessage.READY, 2, _ready_payload()))
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
    assert core.armed_invocation == _INVOCATION


def _session_ready_for_terminal_record(core: _Core) -> CoreRunnerSession:
    session = _session(core)
    session.accept(_frame(RunnerSessionMessage.INVOCATION_OFFER, 1))
    session.accept(_frame(RunnerSessionMessage.READY, 2, _ready_payload()))
    session.accept(_frame(RunnerSessionMessage.STARTED, 3, (b"\x00" * 8,)))
    session.accept(_frame(RunnerSessionMessage.TERMINAL_AVAILABLE, 4, (b"d" * 64,)))
    return session


def test_core_session_replays_the_same_terminal_record_without_a_second_commit() -> (
    None
):
    core = _Core()
    session = _session_ready_for_terminal_record(core)
    record = _frame(RunnerSessionMessage.TERMINAL_RECORD, 5, (b"record",))

    first = session.accept_terminal_record(record)
    second = session.accept_terminal_record(record)

    assert first == second
    assert core.committed == 1


def test_core_session_refuses_replayed_terminal_record_bytes_at_the_same_sequence() -> (
    None
):
    core = _Core()
    session = _session_ready_for_terminal_record(core)
    session.accept_terminal_record(
        _frame(RunnerSessionMessage.TERMINAL_RECORD, 5, (b"record",))
    )

    with pytest.raises(RunnerSessionRefusal, match="runner-session-replay"):
        session.accept_terminal_record(
            _frame(RunnerSessionMessage.TERMINAL_RECORD, 5, (b"different",))
        )
    assert core.committed == 1


def test_core_session_refuses_a_terminal_record_sequence_gap() -> None:
    core = _Core()
    session = _session_ready_for_terminal_record(core)

    with pytest.raises(RunnerSessionRefusal, match="runner-session-sequence-mismatch"):
        session.accept_terminal_record(
            _frame(RunnerSessionMessage.TERMINAL_RECORD, 6, (b"record",))
        )
    assert core.committed == 0


def test_core_session_refuses_a_terminal_record_binding_mismatch() -> None:
    core = _Core()
    session = _session_ready_for_terminal_record(core)
    foreign = RunnerSessionFrame(
        RunnerSessionMessage.TERMINAL_RECORD,
        5,
        _binding(),
        RunnerInvocationId("C" * 43),
        (b"record",),
    )

    with pytest.raises(RunnerSessionRefusal, match="runner-session-binding-mismatch"):
        session.accept_terminal_record(foreign)
    assert core.committed == 0


def test_core_session_refuses_a_launch_before_ready_without_arming() -> None:
    core = _Core()
    session = _session(core)

    with pytest.raises(RunnerSessionRefusal, match="out-of-order"):
        session.accept(_frame(RunnerSessionMessage.LAUNCH, 1))

    assert core.armed == 0


@pytest.mark.proves("runner-cancel-none")
def test_core_session_persists_one_none_replacement_cancel_before_signalling() -> None:
    core = _Core()
    session = _session(core)

    assert session.accept(_frame(RunnerSessionMessage.INVOCATION_OFFER, 1)) is not None
    assert (
        session.accept(_frame(RunnerSessionMessage.READY, 2, _ready_payload()))
        is not None
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
        _session().cancel()


def test_core_session_duplicate_offer_is_idempotent_without_a_second_arm() -> None:
    core = _Core()
    session = _session(core)
    offer = _frame(RunnerSessionMessage.INVOCATION_OFFER, 1)

    first = session.accept(offer)
    second = session.accept(offer)

    assert first == second
    assert core.armed == 0


def test_core_session_refuses_replayed_bytes_at_the_same_sequence() -> None:
    session = _session()
    session.accept(_frame(RunnerSessionMessage.INVOCATION_OFFER, 1))

    with pytest.raises(RunnerSessionRefusal, match="runner-session-replay"):
        session.accept(_frame(RunnerSessionMessage.READY, 1, (b"x",) * 12))


def test_core_session_refuses_a_sequence_gap() -> None:
    session = _session()

    with pytest.raises(RunnerSessionRefusal, match="runner-session-sequence-mismatch"):
        session.accept(_frame(RunnerSessionMessage.INVOCATION_OFFER, 2))


def test_core_session_refuses_a_frame_invocation_other_than_the_tls_id() -> None:
    core = _Core()
    session = _session(core)
    other = RunnerSessionFrame(
        RunnerSessionMessage.INVOCATION_OFFER,
        1,
        _binding(),
        RunnerInvocationId("C" * 43),
        (),
    )

    with pytest.raises(RunnerSessionRefusal, match="runner-session-binding-mismatch"):
        session.accept(other)
    assert core.armed == 0


def test_core_session_refuses_ready_invocation_that_disagrees_with_tls() -> None:
    core = _Core()
    session = _session(core)
    session.accept(_frame(RunnerSessionMessage.INVOCATION_OFFER, 1))
    other = RunnerSessionFrame(
        RunnerSessionMessage.READY,
        2,
        _binding(),
        RunnerInvocationId("C" * 43),
        _ready_payload(),
    )

    with pytest.raises(RunnerSessionRefusal, match="runner-session-binding-mismatch"):
        session.accept(other)
    assert core.armed == 0
    assert core.armed_invocation is None


def test_ack_payload_mismatch_keeps_the_retained_hash_unconsumed() -> None:
    retained = RunnerTerminalEvidenceHash("d" * 64)

    with pytest.raises(RunnerSessionRefusal, match="runner-ack-hash-mismatch"):
        require_matching_evidence_hash((b"e" * 64,), retained)
    assert require_matching_evidence_hash((b"d" * 64,), retained) == retained


def test_core_session_refuses_a_binding_mismatch() -> None:
    session = _session()
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
    session = _session(core)
    session.accept(_frame(RunnerSessionMessage.INVOCATION_OFFER, 1))

    with pytest.raises(RunnerSessionRefusal, match="runner-arm-conflict"):
        session.accept(_frame(RunnerSessionMessage.READY, 2, _ready_payload()))
    assert core.armed == 0


def test_core_session_absorbs_a_cancel_that_crossed_terminal_available() -> None:
    core = _Core()
    session = _session(core)
    session.accept(_frame(RunnerSessionMessage.INVOCATION_OFFER, 1))
    session.accept(_frame(RunnerSessionMessage.READY, 2, _ready_payload()))
    session.accept(_frame(RunnerSessionMessage.STARTED, 3, (b"\x00" * 8,)))

    cancel = session.cancel()
    readback = session.accept(
        _frame(RunnerSessionMessage.TERMINAL_AVAILABLE, 4, (b"d" * 64,))
    )
    absorbed = session.accept(
        _frame(
            RunnerSessionMessage.REFUSE,
            5,
            (b"runner-cancel-conflict", b"d" * 64),
        )
    )
    acknowledgement = session.accept_terminal_record(
        _frame(RunnerSessionMessage.TERMINAL_RECORD, 6, (b"record",))
    )

    assert cancel.message is RunnerSessionMessage.CANCEL
    assert readback is not None
    assert readback.message is RunnerSessionMessage.READBACK
    assert absorbed is None
    assert acknowledgement.message is RunnerSessionMessage.ACK
    assert (core.cancelled, core.committed) == (1, 1)


def test_core_session_refuses_a_crossing_refusal_with_the_wrong_code() -> None:
    core = _Core()
    session = _session(core)
    session.accept(_frame(RunnerSessionMessage.INVOCATION_OFFER, 1))
    session.accept(_frame(RunnerSessionMessage.READY, 2, _ready_payload()))
    session.accept(_frame(RunnerSessionMessage.STARTED, 3, (b"\x00" * 8,)))
    session.cancel()
    session.accept(_frame(RunnerSessionMessage.TERMINAL_AVAILABLE, 4, (b"d" * 64,)))

    with pytest.raises(RunnerSessionRefusal, match="runner-session-noncanonical"):
        session.accept(
            _frame(
                RunnerSessionMessage.REFUSE,
                5,
                (b"runner-arm-conflict", b"d" * 64),
            )
        )


def test_core_session_refuses_a_crossing_refusal_naming_a_different_evidence() -> None:
    core = _Core()
    session = _session(core)
    session.accept(_frame(RunnerSessionMessage.INVOCATION_OFFER, 1))
    session.accept(_frame(RunnerSessionMessage.READY, 2, _ready_payload()))
    session.accept(_frame(RunnerSessionMessage.STARTED, 3, (b"\x00" * 8,)))
    session.cancel()
    session.accept(_frame(RunnerSessionMessage.TERMINAL_AVAILABLE, 4, (b"d" * 64,)))

    with pytest.raises(RunnerSessionRefusal, match="runner-session-noncanonical"):
        session.accept(
            _frame(
                RunnerSessionMessage.REFUSE,
                5,
                (b"runner-cancel-conflict", b"e" * 64),
            )
        )


def test_core_session_refuses_a_crossing_refusal_with_a_malformed_hash() -> None:
    core = _Core()
    session = _session(core)
    session.accept(_frame(RunnerSessionMessage.INVOCATION_OFFER, 1))
    session.accept(_frame(RunnerSessionMessage.READY, 2, _ready_payload()))
    session.accept(_frame(RunnerSessionMessage.STARTED, 3, (b"\x00" * 8,)))
    session.cancel()
    session.accept(_frame(RunnerSessionMessage.TERMINAL_AVAILABLE, 4, (b"d" * 64,)))

    with pytest.raises(RunnerSessionRefusal, match="runner-session-noncanonical"):
        session.accept(
            _frame(
                RunnerSessionMessage.REFUSE,
                5,
                (b"runner-cancel-conflict", b"not-a-hash"),
            )
        )


def test_core_session_refuses_an_unsolicited_crossing_refusal() -> None:
    core = _Core()
    session = _session(core)
    session.accept(_frame(RunnerSessionMessage.INVOCATION_OFFER, 1))
    session.accept(_frame(RunnerSessionMessage.READY, 2, _ready_payload()))
    session.accept(_frame(RunnerSessionMessage.STARTED, 3, (b"\x00" * 8,)))
    session.accept(_frame(RunnerSessionMessage.TERMINAL_AVAILABLE, 4, (b"d" * 64,)))

    with pytest.raises(RunnerSessionRefusal, match="runner-session-out-of-order"):
        session.accept(
            _frame(
                RunnerSessionMessage.REFUSE,
                5,
                (b"runner-cancel-conflict", b"d" * 64),
            )
        )


def test_core_session_refuses_cancel_after_terminal_evidence() -> None:
    session = _session()
    session.accept(_frame(RunnerSessionMessage.INVOCATION_OFFER, 1))
    session.accept(_frame(RunnerSessionMessage.READY, 2, _ready_payload()))
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
    session = _session(core)
    session.accept(_frame(RunnerSessionMessage.INVOCATION_OFFER, 1))
    session.accept(_frame(RunnerSessionMessage.READY, 2, _ready_payload()))
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
    assert len(RUNNER_SESSION_REFUSAL_CODES) == 37


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


def test_prepare_payload_round_trips_the_bound_request() -> None:
    request = _free_request()
    reference = free_runner_auth_reference(request.resolved_binding.auth_profile).value
    payload = encode_runner_prepare_payload(request, reference)

    assert decode_runner_prepare_payload(payload, request.request_hash) == request
    assert payload[PREPARE_AUTH_REFERENCE_FIELD] == reference.encode("ascii")


def test_core_session_refuses_attestation_mismatch_without_arming() -> None:
    core = _Core()
    session = _session(core)
    session.accept(_frame(RunnerSessionMessage.INVOCATION_OFFER, 1))
    payload = list(_ready_payload())
    payload[2] = struct.pack(">Q", 1)

    with pytest.raises(RunnerSessionRefusal, match="runner-attestation-mismatch"):
        session.accept(_frame(RunnerSessionMessage.READY, 2, tuple(payload)))
    assert core.armed == 0


def test_core_session_refuses_manifest_mismatch_without_arming() -> None:
    core = _Core()
    session = _session(core)
    session.accept(_frame(RunnerSessionMessage.INVOCATION_OFFER, 1))
    payload = list(_ready_payload())
    payload[0] = b"not-the-selected-executor"

    with pytest.raises(RunnerSessionRefusal, match="runner-manifest-mismatch"):
        session.accept(_frame(RunnerSessionMessage.READY, 2, tuple(payload)))
    assert core.armed == 0


def test_core_session_refuses_a_measured_cli_the_manifest_executor_runs_none_of() -> (
    None
):
    """The fake-free executor starts no provider CLI, so any version is drift."""
    core = _Core()
    session = _session(core)
    session.accept(_frame(RunnerSessionMessage.INVOCATION_OFFER, 1))

    with pytest.raises(RunnerSessionRefusal, match="runner-provider-cli-drift"):
        session.accept(
            _frame(
                RunnerSessionMessage.READY,
                2,
                _ready_payload(MeasuredProviderCli((2, 1, 233))),
            )
        )
    assert core.armed == 0


def test_a_ready_without_the_measured_provider_cli_field_is_refused() -> None:
    manifest = _candidate_manifest()
    reference = free_runner_auth_reference(
        _free_request().resolved_binding.auth_profile
    )

    with pytest.raises(RunnerSessionRefusal, match="runner-session-noncanonical"):
        require_ready_matches_manifest(
            _ready_payload()[:-1],
            manifest,
            reference.value,
            runner_executor_cli_pin(manifest),
        )


def test_core_session_refuses_a_ready_whose_measurement_is_not_a_version() -> None:
    core = _Core()
    session = _session(core)
    session.accept(_frame(RunnerSessionMessage.INVOCATION_OFFER, 1))
    payload = list(_ready_payload())
    payload[11] = b"whatever-the-runner-felt-like"

    with pytest.raises(RunnerSessionRefusal, match="runner-session-noncanonical"):
        session.accept(_frame(RunnerSessionMessage.READY, 2, tuple(payload)))
    assert core.armed == 0


def test_the_pinned_claude_toolchain_admits_exactly_the_adapter_conformance_set() -> (
    None
):
    """The runner-side pin is the adapter's own reviewed set, never a copy."""
    manifest = replace(
        _candidate_manifest(),
        provider_id=CLAUDE_SUBSCRIPTION_EXECUTOR_KEY.provider_id.value,
        executor_revision=CLAUDE_SUBSCRIPTION_EXECUTOR_KEY.executor_revision.value,
    )
    pin = runner_executor_cli_pin(manifest)

    assert not pin.admits(ABSENT_PROVIDER_CLI)
    assert all(
        pin.admits(MeasuredProviderCli(version))
        for version in CONFORMANT_CLAUDE_VERSIONS
    )


def test_a_manifest_naming_a_revision_this_image_lacks_is_refused_before_ready() -> (
    None
):
    manifest = replace(_candidate_manifest(), executor_revision="claude-someday/v9")

    with pytest.raises(RunnerToolchainUnpinned, match="runner-toolchain-unpinned"):
        runner_executor_cli_pin(manifest)


def test_runner_fence_completes_a_frame_split_across_poll_timeouts() -> None:
    core_side, runner_side = socket.socketpair()
    with core_side, runner_side:
        fence = _CoreFrameFence(runner_side, _binding(), _INVOCATION)
        wire = encode_runner_session_frame(
            _frame(
                RunnerSessionMessage.CANCEL,
                1,
                (b"run", b"cancel-command", b"\x00" * 8, b"NONE"),
            )
        )
        core_side.sendall(wire[:10])

        with pytest.raises(TimeoutError):
            fence.read_frame(timeout=0.05)
        core_side.sendall(wire[10:])

        assert fence.read_frame(timeout=0.05).message is RunnerSessionMessage.CANCEL


def test_runner_fence_refuses_a_core_frame_with_a_foreign_binding() -> None:
    core_side, runner_side = socket.socketpair()
    with core_side, runner_side:
        fence = _CoreFrameFence(runner_side, _binding(), _INVOCATION)
        foreign = RunnerSessionFrame(
            RunnerSessionMessage.LAUNCH,
            1,
            _binding(),
            RunnerInvocationId("C" * 43),
            (),
        )
        core_side.sendall(encode_runner_session_frame(foreign))

        with pytest.raises(RuntimeError, match="runner-session-binding-mismatch"):
            fence.read_frame()


def test_runner_fence_refuses_a_core_sequence_gap() -> None:
    core_side, runner_side = socket.socketpair()
    with core_side, runner_side:
        fence = _CoreFrameFence(runner_side, _binding(), _INVOCATION)
        core_side.sendall(
            encode_runner_session_frame(_frame(RunnerSessionMessage.LAUNCH, 2))
        )

        with pytest.raises(RuntimeError, match="runner-session-sequence-mismatch"):
            fence.read_frame()


def _identity_leaf_for_uri(uri: str) -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "runner-candidate")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(minutes=5))
        .add_extension(
            x509.SubjectAlternativeName([x509.UniformResourceIdentifier(uri)]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM)


def _published_identity_directory(
    tmp_path: Path, invocation: RunnerInvocationId
) -> Path:
    """An identity volume an external issuer already finished publishing.

    Only the two fields `_invocation_from_published_identity` reads --
    `client.crt` and the `ready` marker -- exist; the private key and CA that
    a real TLS handshake needs belong to `load_published_identity`'s own
    tests, not this invocation-derivation one.
    """
    identity = tmp_path / "identity"
    identity.mkdir()
    (identity / "client.crt").write_bytes(
        _identity_leaf_for_uri(runner_uri_for_invocation(_binding(), invocation))
    )
    (identity / "ready").write_bytes(b"")
    return identity


def test_invocation_for_session_mints_the_first_invocation_when_nothing_is_published(
    tmp_path: Path,
) -> None:
    unpublished = tmp_path / "identity"
    unpublished.mkdir()

    minted = _invocation_for_session(unpublished, _binding())

    assert minted.value
    assert _invocation_for_session(unpublished, _binding()).value != minted.value


def test_invocation_for_session_reuses_a_published_identitys_invocation(
    tmp_path: Path,
) -> None:
    identity = _published_identity_directory(tmp_path, _INVOCATION)

    assert _invocation_for_session(identity, _binding()) == _INVOCATION


def test_candidate_scenario_is_a_closed_declared_vocabulary() -> None:
    assert _declared_scenario({"scenario": "cancel"}) is CandidateScenario.CANCEL
    assert _declared_scenario({"scenario": "success"}) is CandidateScenario.SUCCESS
    with pytest.raises(ValueError):
        _declared_scenario({"scenario": "surprise"})


def _self_signed_identity() -> tuple[bytes, bytes]:
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "runner-candidate")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(minutes=5))
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM), key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def test_client_identity_loads_from_the_validated_bytes_without_a_path_reread(
    tmp_path: Path,
) -> None:
    certificate, key = _self_signed_identity()
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)

    _load_verified_client_identity(context, certificate, key, tmp_path)

    with pytest.raises(ssl.SSLError):
        _load_verified_client_identity(
            context, b"not-a-certificate", b"not-a-key", tmp_path
        )
