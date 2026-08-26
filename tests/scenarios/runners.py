from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol

import sqlalchemy as sa

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.run_store import run_from_record_with_bindings
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import runs as runs_table
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.free_runner_executor import (
    FreeRunnerExecutorFactory,
    FreeRunnerHoldJob,
    FreeRunnerPrintJob,
    encode_free_runner_job,
    free_runner_auth_reference,
    refuse_unbound_runner_a_request,
)
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.application.run_runner_session import encode_runner_ready_payload
from atelier2.contracts.agent_attempts import (
    AgentAttemptId,
    RunnerBindingConflict,
    RunnerGenerationBinding,
    RunnerGenerationId,
    RunnerInvocationId,
    RunnerProviderResult,
    RunnerTerminalEvidenceAckTombstone,
    RunnerTerminalEvidenceEnvelope,
    RunnerTerminalEvidenceHash,
    RunnerTerminalEvidenceReadback,
)
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutionRequestV2,
    AgentExecutionResult,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.executions import AgentAttemptExecution, NodeExecutionId
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.run_bindings import RunV3
from atelier2.contracts.runner_manifests import (
    ABSENT_PROVIDER_CLI,
    RunnerManifestV1,
    candidate_runner_manifest,
)
from atelier2.contracts.runner_sessions import RunnerSessionFrame, RunnerSessionMessage
from atelier2.contracts.runner_terminal_evidence_codec import (
    RunnerTerminalEvidenceRecordMissing,
    decode_runner_terminal_evidence_record,
    encode_runner_terminal_evidence_record,
)
from atelier2.contracts.runs import RunId, WorkflowRevision
from atelier2.ports.agent_attempts import (
    RunnerTerminalEvidenceAcknowledgement,
    RunnerTerminalEvidenceAcknowledgementUnavailable,
    RunnerTerminalEvidenceSourceReadback,
)
from atelier2.ports.agent_executions import AgentExecutorRegistry
from atelier2.ports.durable_runs import DurableRunCreated, StartPublishedRunRequestV2


class SimulatedRunnerCrash(RuntimeError):
    pass


@dataclass
class _AcceptedGeneration:
    binding: RunnerGenerationBinding
    invocation_id: RunnerInvocationId
    launched: bool = False
    record: bytes | None = None
    acknowledgement_calls: int = 0
    garbage_collection_count: int = 0
    fail_before_readback: bool = False
    fail_after_acknowledge: bool = False
    fail_acknowledgement: bool = False
    acknowledge_entered: threading.Event | None = None
    acknowledge_release: threading.Event | None = None
    readback_probe: Callable[[], None] | None = None
    acknowledgement_probe: Callable[[], None] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class FakeRunner:
    """One in-memory Runner journal, synchronized independently per generation."""

    def __init__(self) -> None:
        self._accepted: dict[RunnerGenerationId, _AcceptedGeneration] = {}
        self._registry_lock = threading.Lock()

    def accept(self, binding: RunnerGenerationBinding) -> RunnerInvocationId:
        with self._registry_lock:
            accepted = self._accepted.get(binding.generation_id)
            if accepted is not None:
                self._require_binding(accepted, binding)
                return accepted.invocation_id
            invocation_id = RunnerInvocationId(
                f"runner-invocation-{binding.generation_id.value}"
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
        accepted = self._accepted[binding.generation_id]
        with accepted.lock:
            self._require_binding(accepted, binding)
            if accepted.invocation_id != invocation_id:
                raise RunnerBindingConflict(
                    "runner launch differs from accepted generation"
                )
            accepted.launched = True

    def observe(
        self, envelope: RunnerTerminalEvidenceEnvelope
    ) -> RunnerTerminalEvidenceEnvelope:
        accepted = self._accepted[envelope.binding.generation_id]
        with accepted.lock:
            if accepted.binding != envelope.binding:
                raise RunnerBindingConflict(
                    "runner evidence differs from accepted binding"
                )
            if envelope.invocation_id not in (None, accepted.invocation_id):
                raise RunnerBindingConflict("runner evidence differs from invocation")
            decoded = decode_runner_terminal_evidence_record(accepted.record)
            if isinstance(decoded, RunnerTerminalEvidenceAckTombstone):
                raise RunnerBindingConflict("runner evidence was already acknowledged")
            if isinstance(decoded, RunnerTerminalEvidenceEnvelope):
                return decoded
            if not isinstance(decoded, RunnerTerminalEvidenceRecordMissing):
                raise RunnerBindingConflict("runner evidence record is unusable")
            accepted.record = encode_runner_terminal_evidence_record(envelope)
            return envelope

    def readback(
        self, binding: RunnerGenerationBinding
    ) -> RunnerTerminalEvidenceSourceReadback:
        accepted = self._accepted[binding.generation_id]
        with accepted.lock:
            self._require_binding(accepted, binding)
            if accepted.fail_before_readback:
                accepted.fail_before_readback = False
                raise SimulatedRunnerCrash("before terminal-evidence readback")
            if accepted.readback_probe is not None:
                accepted.readback_probe()
            readback = decode_runner_terminal_evidence_record(accepted.record)
            if isinstance(
                readback,
                (RunnerTerminalEvidenceEnvelope, RunnerTerminalEvidenceAckTombstone),
            ):
                self._require_readback_binding(readback, binding)
            return readback

    def acknowledge(
        self,
        envelope: RunnerTerminalEvidenceEnvelope,
        accepted_hash: RunnerTerminalEvidenceHash,
    ) -> RunnerTerminalEvidenceAcknowledgement:
        accepted = self._accepted[envelope.binding.generation_id]
        with accepted.lock:
            self._require_binding(accepted, envelope.binding)
            accepted.acknowledgement_calls += 1
            if accepted.fail_acknowledgement:
                accepted.fail_acknowledgement = False
                return RunnerTerminalEvidenceAcknowledgementUnavailable()
            if accepted.acknowledgement_probe is not None:
                accepted.acknowledgement_probe()
            evidence_hash = RunnerTerminalEvidenceHash.for_envelope(envelope)
            if accepted_hash != evidence_hash:
                raise RunnerBindingConflict("runner ACK hash differs from evidence")
            expected = RunnerTerminalEvidenceAckTombstone(
                envelope.binding, envelope.invocation_id, accepted_hash
            )
            retained = decode_runner_terminal_evidence_record(accepted.record)
            if isinstance(retained, RunnerTerminalEvidenceAckTombstone):
                if retained != expected:
                    raise RunnerBindingConflict("runner ACK differs from tombstone")
            elif isinstance(retained, RunnerTerminalEvidenceEnvelope):
                if retained != envelope:
                    raise RunnerBindingConflict("runner ACK differs from evidence")
                accepted.record = encode_runner_terminal_evidence_record(expected)
                accepted.garbage_collection_count += 1
            else:
                raise RunnerBindingConflict("runner ACK has no usable evidence")

            if accepted.acknowledge_entered is not None:
                accepted.acknowledge_entered.set()
                release = accepted.acknowledge_release
                if release is None or not release.wait(timeout=5):
                    raise AssertionError("scenario Runner ACK barrier was not released")
            if accepted.fail_after_acknowledge:
                accepted.fail_after_acknowledge = False
                raise SimulatedRunnerCrash("after terminal-evidence ACK and GC")
            return expected

    def observed_evidence(
        self, binding: RunnerGenerationBinding
    ) -> RunnerTerminalEvidenceEnvelope | None:
        accepted = self._accepted[binding.generation_id]
        with accepted.lock:
            self._require_binding(accepted, binding)
            readback = decode_runner_terminal_evidence_record(accepted.record)
            return (
                readback
                if isinstance(readback, RunnerTerminalEvidenceEnvelope)
                else None
            )

    def retain_record(
        self, binding: RunnerGenerationBinding, record: bytes | None
    ) -> None:
        accepted = self._accepted[binding.generation_id]
        with accepted.lock:
            self._require_binding(accepted, binding)
            accepted.record = record

    def record_bytes(self, binding: RunnerGenerationBinding) -> bytes | None:
        accepted = self._accepted[binding.generation_id]
        with accepted.lock:
            self._require_binding(accepted, binding)
            return accepted.record

    def fail_next_readback(self, binding: RunnerGenerationBinding) -> None:
        self._accepted[binding.generation_id].fail_before_readback = True

    def fail_after_next_acknowledge(self, binding: RunnerGenerationBinding) -> None:
        self._accepted[binding.generation_id].fail_after_acknowledge = True

    def fail_next_acknowledgement(self, binding: RunnerGenerationBinding) -> None:
        self._accepted[binding.generation_id].fail_acknowledgement = True

    def hold_acknowledge(
        self,
        binding: RunnerGenerationBinding,
        entered: threading.Event,
        release: threading.Event,
    ) -> None:
        accepted = self._accepted[binding.generation_id]
        accepted.acknowledge_entered = entered
        accepted.acknowledge_release = release

    def probe_calls(
        self,
        binding: RunnerGenerationBinding,
        readback: Callable[[], None],
        acknowledge: Callable[[], None],
    ) -> None:
        accepted = self._accepted[binding.generation_id]
        accepted.readback_probe = readback
        accepted.acknowledgement_probe = acknowledge

    def acknowledgement_count(self, binding: RunnerGenerationBinding) -> int:
        return self._accepted[binding.generation_id].acknowledgement_calls

    def garbage_collection_count(self, binding: RunnerGenerationBinding) -> int:
        return self._accepted[binding.generation_id].garbage_collection_count

    @property
    def accepted_generation_ids(self) -> frozenset[RunnerGenerationId]:
        return frozenset(self._accepted)

    @property
    def provider_start_count(self) -> int:
        return sum(accepted.launched for accepted in self._accepted.values())

    @staticmethod
    def _require_binding(
        accepted: _AcceptedGeneration, binding: RunnerGenerationBinding
    ) -> None:
        if accepted.binding != binding:
            raise RunnerBindingConflict("runner generation is bound to different work")

    @staticmethod
    def _require_readback_binding(
        readback: RunnerTerminalEvidenceReadback,
        binding: RunnerGenerationBinding,
    ) -> None:
        if readback.binding != binding:
            raise RunnerBindingConflict(
                "runner readback differs from requested binding"
            )


# ---------------------------------------------------------------------------
# A durable, prepared free-runner attempt and a scripted Runner peer for it
# (`#540` C-3.4) -- reused by `execute_agent_attempt_on_runner`'s own
# integration test to durably prepare a run exactly as
# `tests/witness/runner_candidate_core.py`'s Core process does, and to play
# the Runner side of one session directly against a real `CoreRunnerSession`
# without a socket. The transport and wire protocol themselves are already
# pinned by `tests/integration/test_runner_session_wire.py`; what these
# helpers exist for is standing up a caller's own composition around an
# already-accepted session.
# ---------------------------------------------------------------------------

FREE_RUNNER_OUTPUT_SCHEMA = PublishedRevision(RevisionKind.SCHEMA, b"true")


def _free_runner_workflow_document(
    job: FreeRunnerPrintJob | FreeRunnerHoldJob,
) -> bytes:
    """The one-node workflow whose authored instruction is the job document --
    the same shape `tests/witness/runner_candidate_core.py::_document_for`
    builds, generalized for reuse."""
    instruction_literal = json.dumps(encode_free_runner_job(job).decode("utf-8"))
    return (
        b"format_version: 3\n"
        b"name: Prepared free Runner attempt\n"
        b"nodes:\n"
        b"  - id: execute\n"
        b"    type: agent\n"
        b"    role: runner\n"
        b"    mode: headless\n"
        b"    instruction: " + instruction_literal.encode("utf-8") + b"\n"
        b"    outputs:\n"
        b"      - name: result\n"
        b"        schema:\n"
        b"          ref: result-schema\n"
        b"          revision: "
        + FREE_RUNNER_OUTPUT_SCHEMA.revision_hash.value.encode("ascii")
        + b"\n"
    )


@dataclass(frozen=True, slots=True)
class PreparedFreeRunnerAttempt:
    """A durable V3 run with one free-runner agent node, started through to
    its one attempt's readiness -- everything a Runner-lease driver needs
    durably true before it ever calls `store.prepare`."""

    runtime: DbosRuntime
    store: DbosAgentAttemptStore
    execution: AgentAttemptExecution
    auth_reference: str


def prepared_free_runner_attempt(
    root: Path,
    run_id_value: str,
    job: FreeRunnerPrintJob | FreeRunnerHoldJob,
) -> PreparedFreeRunnerAttempt:
    workspace = root / "workspace"
    workspace.mkdir(mode=0o700, exist_ok=True)
    runtime = DbosRuntime(
        DbosRuntimeSettings(
            root / "core.sqlite3",
            "execute-agent-attempt-on-runner-test",
            agent_scratch_root=workspace,
        ),
        LoopbackEffectAdapterFactory(
            root / "effects.sqlite3",
            AdapterRevision("execute-agent-attempt-on-runner-test/v1"),
            EffectDestination("execute-agent-attempt-on-runner-test"),
        ),
        ExactOutputAgentExecutorFactory(),
        (),
    )
    runtime.initialize_storage()
    DbosCatalogStore(runtime.engine).publish_revision(FREE_RUNNER_OUTPUT_SCHEMA)
    runner_registry = AgentExecutorRegistry((FreeRunnerExecutorFactory(),))
    catalog = DbosAgentConfigurationCatalog(runtime.engine, runner_registry)
    auth = AuthProfileRevision(
        "candidate", 1, ProviderId("fake-free"), AuthMode.API_KEY
    )
    catalog.publish_auth_profile_revision(auth)
    configuration = AgentConfigurationRevision(
        "free",
        auth.revision_hash,
        AgentExecutorRevision("fake-free/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    catalog.publish_agent_configuration_revision(configuration)
    workflow = WorkflowRevision(_free_runner_workflow_document(job))
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    run_id = RunId(run_id_value)
    started = DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        runner_registry,
        effect_adapter_proves_absence=True,
    ).start_published(
        StartPublishedRunRequestV2(
            run_id,
            workflow.revision_hash,
            AgentBindingSet(
                (AgentBinding(AgentRole("runner"), configuration.revision_hash),)
            ),
        )
    )
    if not isinstance(started, DurableRunCreated):
        raise TypeError(f"scenario run could not start: {started!r}")
    with runtime.engine.connect() as connection:
        record = (
            connection.execute(
                sa.select(runs_table).where(runs_table.c.run_id == run_id.value)
            )
            .mappings()
            .one()
        )
        run = run_from_record_with_bindings(connection, record)
    if not isinstance(run, RunV3):
        raise TypeError("scenario run did not resolve its V3 run")
    request = AgentExecutionRequestV2(
        NodeExecutionId.for_node(run_id, workflow.revision_hash, "execute"),
        run_id,
        workflow.revision_hash,
        "execute",
        run.agent_bindings[0],
        AgentExecutorOperationalIdentity("free-runner-candidate"),
        encode_free_runner_job(job),
    )
    refuse_unbound_runner_a_request(request)
    execution = AgentAttemptExecution(
        request,
        AgentAttemptId.for_execution(request.node_execution_id, request.request_hash),
        1,
    )
    store = DbosAgentAttemptStore(runtime.engine)
    reference = free_runner_auth_reference(request.resolved_binding.auth_profile).value
    return PreparedFreeRunnerAttempt(runtime, store, execution, reference)


def free_runner_candidate_manifest(**overrides: object) -> RunnerManifestV1:
    """A manifest for the fixed free-runner candidate program -- self-
    consistent for a scripted exchange this module fully controls, rather
    than measured against a live host the way
    `tests/integration/test_runner_session_wire.py::_host_manifest` is."""
    manifest = candidate_runner_manifest(
        source_commit="a" * 40,
        image_digest="sha256:" + "b" * 64,
        required_landlock_abi=1,
        executor_revision="fake-free/v1",
        executor_operational_identity="free-runner-candidate",
        provider_id="fake-free",
        auth_mode="api_key",
        requested_capability="headless",
    )
    return replace(manifest, **overrides) if overrides else manifest


class RunnerSessionAdvancerLike(Protocol):
    """The narrow shape `drive_free_runner_session_to_released` needs from a
    session -- structurally `CoreRunnerSession`, declared here so this
    scenario module names no application type of its own."""

    def accept(self, frame: RunnerSessionFrame) -> RunnerSessionFrame | None: ...

    def accept_terminal_record(
        self, frame: RunnerSessionFrame
    ) -> RunnerSessionFrame | None: ...


def drive_free_runner_session_to_started(
    session: RunnerSessionAdvancerLike,
    binding: RunnerGenerationBinding,
    invocation_id: RunnerInvocationId,
    manifest: RunnerManifestV1,
    auth_reference: str,
) -> None:
    """Play the Runner side only as far as STARTED, and stop there.

    The half of a session that a launcher and its Runner have finished before
    any terminal evidence exists: Core has armed this invocation and the provider
    is running. A scenario that wants the shape a Serve crash finds mid-session --
    an Attempt durably armed, its lease claimed, its ending still to come -- stops
    here and lays the Runner's retained record in the handoff itself, exactly as
    the launcher's own journal would have.

    `drive_free_runner_session_to_released` opens with these same frames, and
    calls this rather than repeating them: one owner for the beginning of a
    session means a scenario that stops early and one that runs through cannot
    disagree about what a Runner said first.
    """

    def _frame(
        message: RunnerSessionMessage,
        sequence: int,
        payload: tuple[bytes, ...] = (),
    ) -> RunnerSessionFrame:
        return RunnerSessionFrame(message, sequence, binding, invocation_id, payload)

    session.accept(_frame(RunnerSessionMessage.INVOCATION_OFFER, 1))
    session.accept(
        _frame(
            RunnerSessionMessage.READY,
            2,
            encode_runner_ready_payload(manifest, auth_reference, ABSENT_PROVIDER_CLI),
        )
    )
    session.accept(_frame(RunnerSessionMessage.STARTED, 3, (b"\x00" * 8,)))


def drive_free_runner_session_to_released(
    session: RunnerSessionAdvancerLike,
    binding: RunnerGenerationBinding,
    invocation_id: RunnerInvocationId,
    manifest: RunnerManifestV1,
    auth_reference: str,
    output_bytes: bytes,
    *,
    resend_as_tombstone: bool = False,
) -> RunnerTerminalEvidenceEnvelope:
    """Play the Runner side of one free-runner session directly against a
    real, already-accepted `CoreRunnerSession`, reaching RELEASED without a
    socket. Returns the terminal evidence envelope this exchange committed
    (its content, not necessarily the exact bytes on the wire -- see
    `resend_as_tombstone`), so a caller can assert against it.

    `resend_as_tombstone` plays a *resumed* candidate: one whose journal
    already tombstoned this exact evidence because it received Core's ACK
    before this exact Core process died. TERMINAL_AVAILABLE still offers the
    same evidence hash -- the candidate's journal fixed that fact before the
    resume, same as any other reconnect -- but TERMINAL_RECORD carries the
    tombstone in place of the envelope, because the envelope itself is gone
    from the journal by then.
    """

    def _frame(
        message: RunnerSessionMessage,
        sequence: int,
        payload: tuple[bytes, ...] = (),
    ) -> RunnerSessionFrame:
        return RunnerSessionFrame(message, sequence, binding, invocation_id, payload)

    drive_free_runner_session_to_started(
        session, binding, invocation_id, manifest, auth_reference
    )
    envelope = RunnerTerminalEvidenceEnvelope(
        binding,
        invocation_id,
        RunnerProviderResult(AgentExecutionResult(output_bytes)),
    )
    evidence_hash = RunnerTerminalEvidenceHash.for_envelope(envelope)
    tombstone = RunnerTerminalEvidenceAckTombstone(
        binding, invocation_id, evidence_hash
    )
    session.accept(
        _frame(
            RunnerSessionMessage.TERMINAL_AVAILABLE,
            4,
            (evidence_hash.value.encode("ascii"),),
        )
    )
    terminal_record_payload = (
        encode_runner_terminal_evidence_record(tombstone)
        if resend_as_tombstone
        else encode_runner_terminal_evidence_record(envelope)
    )
    session.accept_terminal_record(
        _frame(RunnerSessionMessage.TERMINAL_RECORD, 5, (terminal_record_payload,))
    )
    session.accept(
        _frame(
            RunnerSessionMessage.ACK_TOMBSTONE,
            6,
            (encode_runner_terminal_evidence_record(tombstone),),
        )
    )
    session.accept(_frame(RunnerSessionMessage.RELEASED, 7, (b"released",)))
    return envelope
