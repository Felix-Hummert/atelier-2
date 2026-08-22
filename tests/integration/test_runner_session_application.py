from __future__ import annotations

import socket
import ssl
import struct
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from atelier2.adapters.free_runner_executor import (
    FreeRunnerAuthorizationResolver,
    refuse_unbound_runner_a_request,
)
from atelier2.application.run_runner_session import (
    CoreRunnerSession,
    RunnerSessionRefusal,
    cancellation_refusal_code,
    decode_runner_prepare_payload,
    encode_runner_prepare_payload,
    encode_runner_ready_payload,
    require_matching_evidence_hash,
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
    _load_verified_client_identity,
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
    reference = FreeRunnerAuthorizationResolver().reference_for(
        request.resolved_binding.auth_profile
    )
    return CoreRunnerSession(
        _binding(),
        core if core is not None else _Core(),
        encode_runner_prepare_payload(request, reference),
        _candidate_manifest(),
        reference,
        _INVOCATION,
    )


def _ready_payload() -> tuple[bytes, ...]:
    request = _free_request()
    reference = FreeRunnerAuthorizationResolver().reference_for(
        request.resolved_binding.auth_profile
    )
    return encode_runner_ready_payload(_candidate_manifest(), reference)


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
        session.accept(_frame(RunnerSessionMessage.READY, 1, (b"x",) * 11))


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


def test_prepare_payload_round_trips_the_bound_request() -> None:
    request = _free_request()
    reference = FreeRunnerAuthorizationResolver().reference_for(
        request.resolved_binding.auth_profile
    )
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
