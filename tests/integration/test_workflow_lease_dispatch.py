"""`durable_node`/`durable_agent_attempt_replacement` dispatch by carrier
(`#540` C-3.6): a `RUNNER_LEASE`-carried key runs over
`atelier2.application.execute_agent_attempt_on_runner` and never touches
Serve's own process supervisor; a `LOCAL_PROCESS`-carried key runs exactly as
before. Both are proven through the real `atelier2.adapters.dbos.runtime`
composition and the real DBOS-driven workflow -- `runtime.launch()`, a
published run, and a poll for the durable state an operator would read --
with only the Runner-lease session's own socket/TLS transport scripted, the
same layering `tests/integration/test_execute_agent_attempt_on_runner.py`
already established for the driver underneath it.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Never

import pytest
import sqlalchemy as sa
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from sqlalchemy.engine import Engine

import atelier2.adapters.dbos.runtime as dbos_runtime
from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.runner_session_core import DbosRunnerSessionCore
from atelier2.adapters.dbos.runtime import (
    DbosRuntime,
    DbosRuntimeSettings,
    runner_lease_cancellation_command_id,
)
from atelier2.adapters.dbos.schema import runs
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.file_runner_leases import FileRunnerLeasePublisher
from atelier2.adapters.free_runner_executor import (
    FreeRunnerExecutorFactory,
    FreeRunnerPrintJob,
    encode_free_runner_job,
    free_runner_auth_reference,
)
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.adapters.runner_cli_pins import runner_executor_cli_pin
from atelier2.application.execute_agent_attempt_on_runner import (
    ExecuteAgentAttemptOnRunnerOutcome,
    RunnerAttemptLeaseMaterial,
    execute_agent_attempt_on_runner,
)
from atelier2.contracts.agent_attempts import RunnerInvocationId
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutorRevision,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.executions import AgentAttemptExecution
from atelier2.contracts.runner_leases import RunnerLeaseId
from atelier2.contracts.runner_manifests import RunnerManifestV1, runner_manifest_id
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
from atelier2.ports.agent_attempts import RunnerTerminalEvidenceStore
from atelier2.ports.agent_executions import (
    AgentExecutorCarrier,
    AgentExecutorRegistration,
)
from atelier2.ports.durable_runs import DurableRunCreated, StartPublishedRunRequestV2
from tests.scenarios.agents import RecordingAgentExecutorFactoryV2, agent_scratch_root
from tests.scenarios.runners import (
    RunnerSessionAdvancerLike,
    drive_free_runner_session_to_released,
    free_runner_candidate_manifest,
)
from tests.scenarios.workflows import ANY_JSON_SCHEMA, declared_output

_ACCEPT_TIMEOUT_SECONDS = 5.0
_WAIT_SECONDS = 16.0
_POLL_SECONDS = 0.025
_LOCAL_PROCESS_OUTPUT = b'"local process result"'
_LEASE_OUTPUT = b'"runner lease result"'
_FREE_RUNNER_AUTH_PROFILE = AuthProfileRevision(
    "candidate", 1, ProviderId("fake-free"), AuthMode.API_KEY
)
_LOCAL_PROCESS_AUTH_PROFILE = AuthProfileRevision(
    "reviewer-key", 1, ProviderId("recording"), AuthMode.SUBSCRIPTION
)
_FREE_RUNNER_CONFIGURATION = AgentConfigurationRevision(
    "free",
    _FREE_RUNNER_AUTH_PROFILE.revision_hash,
    AgentExecutorRevision("fake-free/v1"),
    AgentExecutionCapability.HEADLESS,
    AgentConfigurationRevisionFormatVersion.V2,
)
_LOCAL_PROCESS_CONFIGURATION = AgentConfigurationRevision(
    "recorded",
    _LOCAL_PROCESS_AUTH_PROFILE.revision_hash,
    AgentExecutorRevision("recording/v1"),
    AgentExecutionCapability.HEADLESS,
    AgentConfigurationRevisionFormatVersion.V2,
)


def _self_signed_identity(directory: Path) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-core")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    certificate_pem = certificate.public_bytes(serialization.Encoding.PEM)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "core.key").write_bytes(key_pem)
    (directory / "core.crt").write_bytes(certificate_pem)
    (directory / "ca.crt").write_bytes(certificate_pem)


def _free_runner_document(text: str) -> bytes:
    instruction_literal = json.dumps(
        encode_free_runner_job(FreeRunnerPrintJob(text)).decode("utf-8")
    )
    return (
        b"format_version: 3\n"
        b"name: Dispatch scenario over a Runner lease\n"
        b"nodes:\n"
        b"  - id: execute\n"
        b"    type: agent\n"
        b"    role: runner\n"
        b"    mode: headless\n"
        b"    instruction: " + instruction_literal.encode("utf-8") + b"\n"
    ) + declared_output()


_LOCAL_PROCESS_DOCUMENT = (
    b"format_version: 3\n"
    b"name: Dispatch scenario over the local process\n"
    b"nodes:\n"
    b"  - id: execute\n"
    b"    type: agent\n"
    b"    role: reviewer\n"
    b"    mode: headless\n"
    b"    instruction: Say what a local process executor answers.\n"
) + declared_output()


def _released_event() -> threading.Event:
    event = threading.Event()
    event.set()
    return event


@dataclass
class _DriveTracking:
    """What every scripted lease drive shares: whether two ever overlapped."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    active: int = 0
    peak: int = 0
    completed_attempt_ids: list[str] = field(default_factory=list)
    # A drive records its overlap, then holds here until released. Set by
    # default so every test but the concurrency proof runs straight through;
    # that proof clears it, waits for the second attempt to log that it is
    # blocked on the runner slot, and only then sets it -- so the slot, not a
    # chance of DBOS serialization, is what keeps `peak` at one.
    released: threading.Event = field(default_factory=_released_event)

    def entered(self) -> None:
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)

    def left(self, attempt_id: str) -> None:
        with self.lock:
            self.active -= 1
            self.completed_attempt_ids.append(attempt_id)


@dataclass
class _ScriptedTransport:
    output_bytes: bytes
    manifest: RunnerManifestV1
    auth_reference: str

    def drive_to_released(
        self,
        binding: object,
        peer: object,
        session_for_first_connection: Callable[
            [RunnerInvocationId], RunnerSessionAdvancerLike
        ],
        on_started: object | None = None,
    ) -> None:
        del peer, on_started
        invocation_id = RunnerInvocationId("A" * 43)
        session = session_for_first_connection(invocation_id)
        drive_free_runner_session_to_released(
            session,
            binding,  # type: ignore[arg-type]
            invocation_id,
            self.manifest,
            self.auth_reference,
            self.output_bytes,
        )


def _spawn_peer_material_writer(
    attempt_root: Path, lease_id: RunnerLeaseId, manifest: RunnerManifestV1
) -> tuple[threading.Thread, list[BaseException]]:
    """A stand-in for the launcher's own handoff, matching `#540` C-3.4's own
    test fixture (`tests/integration/test_execute_agent_attempt_on_runner.py`
    ::`_spawn_peer_material_writer`) -- this dispatch layer never rebuilds the
    Runner-lease transport, only replaces it."""

    errors: list[BaseException] = []

    def _write() -> None:
        try:
            attempt_directory = attempt_root / lease_id.value
            peer_directory = attempt_directory / "peer"
            handoff_directory = attempt_directory / "handoff"
            deadline = time.monotonic() + _ACCEPT_TIMEOUT_SECONDS
            while not (peer_directory.is_dir() and handoff_directory.is_dir()):
                if time.monotonic() > deadline:
                    raise TimeoutError("scenario lease material was never published")
                time.sleep(0.01)
            (peer_directory / "client.crt").write_bytes(b"scenario-peer-leaf")
            (handoff_directory / "inspect-attested").write_text(
                runner_manifest_id(manifest).value, encoding="ascii"
            )
        except BaseException as error:  # noqa: BLE001 -- surfaced to the test thread
            errors.append(error)

    thread = threading.Thread(target=_write)
    thread.start()
    return thread, errors


@dataclass
class _ScriptedLeaseDriver:
    """A `RunnerLeaseAttemptDriver` over the real C-3.4 driver and a real
    on-disk lease directory, with only the socket/TLS transport scripted --
    the seam this dispatch phase, not the transport, is the subject of."""

    store: DbosAgentAttemptStore
    runner_store: RunnerTerminalEvidenceStore
    engine: Engine
    leases: FileRunnerLeasePublisher
    attempt_root: Path
    tracking: _DriveTracking

    def drive(
        self, execution: AgentAttemptExecution
    ) -> ExecuteAgentAttemptOnRunnerOutcome:
        manifest = free_runner_candidate_manifest()
        auth_reference = free_runner_auth_reference(
            execution.request.resolved_binding.auth_profile
        ).value
        material = RunnerAttemptLeaseMaterial(
            manifest,
            "atelier2-runner-candidate:test",
            "serve-test",
            b"ca-placeholder",
            b"core-placeholder",
            b"core-peer-placeholder",
            auth_reference,
            runner_executor_cli_pin(manifest),
        )
        lease_id = RunnerLeaseId(execution.attempt_id.value)
        writer, errors = _spawn_peer_material_writer(
            self.attempt_root, lease_id, manifest
        )
        core = DbosRunnerSessionCore(
            execution,
            self.store,
            runner_lease_cancellation_command_id(execution.attempt_id),
            engine=self.engine,
        )
        self.tracking.entered()
        try:
            self.tracking.released.wait(timeout=_WAIT_SECONDS)
            outcome = execute_agent_attempt_on_runner(
                execution,
                self.store,
                self.runner_store,
                core,
                _ScriptedTransport(_LEASE_OUTPUT, manifest, auth_reference),
                self.leases,
                self.leases,
                material,
                _ACCEPT_TIMEOUT_SECONDS,
            )
        finally:
            self.tracking.left(execution.attempt_id.value)
        writer.join(timeout=_ACCEPT_TIMEOUT_SECONDS)
        assert not errors
        return outcome


def _forbidden(*_args: object, **_kwargs: object) -> Never:
    raise AssertionError(
        "this composition must not have needed local process authority"
    )


def _scripted_driver_factory(
    tracking: _DriveTracking,
) -> Callable[
    [DbosRuntimeSettings, Engine, DbosAgentAttemptStore, FileRunnerLeasePublisher],
    _ScriptedLeaseDriver,
]:
    def build(
        settings: DbosRuntimeSettings,
        engine: Engine,
        store: DbosAgentAttemptStore,
        leases: FileRunnerLeasePublisher,
    ) -> _ScriptedLeaseDriver:
        assert settings.runner_lease_root is not None
        lease_root = settings.runner_lease_root
        return _ScriptedLeaseDriver(
            store, store, engine, leases, lease_root / "attempts", tracking
        )

    return build


def _build_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tracking: _DriveTracking,
    registrations: tuple[AgentExecutorRegistration, ...],
) -> DbosRuntime:
    monkeypatch.setattr(
        dbos_runtime, "_runner_lease_attempt_driver", _scripted_driver_factory(tracking)
    )
    identity = tmp_path / "identity"
    _self_signed_identity(identity)
    runtime = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "lease-dispatch-test",
            agent_scratch_root=agent_scratch_root(tmp_path),
            runner_lease_root=tmp_path / "leases",
            runner_image="atelier2-runner-candidate:test",
            runner_image_digest="sha256:" + "a" * 64,
            runner_console_container="serve-test",
            runner_core_identity_directory=identity,
            runner_accept_timeout_seconds=_ACCEPT_TIMEOUT_SECONDS,
            runner_lease_source_commit="b" * 40,
        ),
        LoopbackEffectAdapterFactory(
            tmp_path / "effects.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("lease-dispatch-test"),
        ),
        ExactOutputAgentExecutorFactory(),
        registrations,
    )
    runtime.initialize_storage()
    return runtime


def _lease_registration(available: bool) -> AgentExecutorRegistration:
    build = (
        AgentExecutorRegistration.startable
        if available
        else AgentExecutorRegistration.unavailable
    )
    return build(FreeRunnerExecutorFactory(), AgentExecutorCarrier.RUNNER_LEASE)


def _local_process_registration(available: bool) -> AgentExecutorRegistration:
    build = (
        AgentExecutorRegistration.startable
        if available
        else AgentExecutorRegistration.unavailable
    )
    return build(
        RecordingAgentExecutorFactoryV2(
            "recording", "recording/v1", "recording-operation", _LOCAL_PROCESS_OUTPUT
        )
    )


def _publish_catalog(runtime: DbosRuntime) -> None:
    DbosCatalogStore(runtime.engine).publish_revision(ANY_JSON_SCHEMA)
    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    for auth, configuration in (
        (_FREE_RUNNER_AUTH_PROFILE, _FREE_RUNNER_CONFIGURATION),
        (_LOCAL_PROCESS_AUTH_PROFILE, _LOCAL_PROCESS_CONFIGURATION),
    ):
        catalog.publish_auth_profile_revision(auth)
        catalog.publish_agent_configuration_revision(configuration)


def _publish_and_start(
    runtime: DbosRuntime,
    run_id: RunId,
    role: str,
    configuration: AgentConfigurationRevision,
    document: bytes,
) -> None:
    workflow = WorkflowRevision(document)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    started = DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        runtime.agent_executor_registry,
        effect_adapter_proves_absence=True,
    ).start_published(
        StartPublishedRunRequestV2(
            run_id,
            workflow.revision_hash,
            AgentBindingSet(
                (AgentBinding(AgentRole(role), configuration.revision_hash),)
            ),
        )
    )
    assert isinstance(started, DurableRunCreated), started


def wait_for_state(runtime: DbosRuntime, run_id: RunId, state: RunState) -> None:
    deadline = time.monotonic() + _WAIT_SECONDS
    observed = ""
    while time.monotonic() < deadline:
        with runtime.engine.connect() as connection:
            observed = str(
                connection.scalar(
                    sa.select(runs.c.state).where(runs.c.run_id == run_id.value)
                )
            )
        if observed == state.value:
            return
        time.sleep(_POLL_SECONDS)
    raise AssertionError(
        f"run {run_id.value!r} stayed {observed!r}, expected {state.value!r}"
    )


def wait_for_drives(tracking: _DriveTracking, count: int) -> None:
    """The durable run reaching a terminal state races this test's own
    `_ScriptedLeaseDriver.drive()` returning: the driver's durable commit
    lands mid-session, before the remaining ACK/RELEASED frames finish, so a
    caller that wants the drive itself to have returned waits for it here
    rather than for the run alone."""

    deadline = time.monotonic() + _WAIT_SECONDS
    while time.monotonic() < deadline:
        if len(tracking.completed_attempt_ids) >= count:
            return
        time.sleep(_POLL_SECONDS)
    raise AssertionError(
        f"only {len(tracking.completed_attempt_ids)} drive(s) returned, expected {count}"
    )


def wait_for_awaiting_runner_slot(caplog: pytest.LogCaptureFixture) -> None:
    """A second Runner-lease attempt logging that it is blocked on the runner
    slot -- the proof the slot itself, not a chance of DBOS running the two
    node workflows one at a time, is what serialized the two drives."""

    deadline = time.monotonic() + _WAIT_SECONDS
    while time.monotonic() < deadline:
        if any(
            getattr(record, "event", None) == "agent_attempt_awaiting_runner_slot"
            for record in caplog.records
        ):
            return
        time.sleep(_POLL_SECONDS)
    raise AssertionError(
        "no second attempt ever blocked on the runner slot; the slot did not "
        "serialize the two concurrent drives"
    )


def test_a_fake_free_node_completes_over_the_lease_path_touching_no_process_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pure Runner-lease deployment: no `LOCAL_PROCESS` key is registered at
    all, so `AgentProcessSupervisor` is never constructed -- structurally, not
    just behaviorally, unreachable -- and the fake-free node still reaches
    `COMPLETED` over the real C-3.4 driver."""

    tracking = _DriveTracking()
    monkeypatch.setattr(dbos_runtime, "AgentProcessSupervisor", _forbidden)
    monkeypatch.setattr(dbos_runtime, "delegated_cgroup_root", _forbidden)
    run_id = RunId("lease-dispatch/fake-free-only")
    runtime = _build_runtime(
        tmp_path, monkeypatch, tracking, (_lease_registration(True),)
    )
    try:
        _publish_catalog(runtime)
        runtime.launch()
        _publish_and_start(
            runtime,
            run_id,
            "runner",
            _FREE_RUNNER_CONFIGURATION,
            _free_runner_document("hello"),
        )
        wait_for_state(runtime, run_id, RunState.COMPLETED)
        wait_for_drives(tracking, 1)
        assert runtime.agent_workspace_owner is None
    finally:
        runtime.close()


def test_a_local_process_node_still_runs_the_process_path_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same registry that also serves a `RUNNER_LEASE` key still drives a
    `LOCAL_PROCESS` node through its untouched process path."""

    tracking = _DriveTracking()
    run_id = RunId("lease-dispatch/local-process-only")
    runtime = _build_runtime(
        tmp_path,
        monkeypatch,
        tracking,
        (_lease_registration(True), _local_process_registration(True)),
    )
    try:
        _publish_catalog(runtime)
        runtime.launch()
        _publish_and_start(
            runtime,
            run_id,
            "reviewer",
            _LOCAL_PROCESS_CONFIGURATION,
            _LOCAL_PROCESS_DOCUMENT,
        )
        wait_for_state(runtime, run_id, RunState.COMPLETED)
        assert not tracking.completed_attempt_ids
    finally:
        runtime.close()


def test_two_concurrent_lease_attempts_both_complete_one_after_another(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A cleared release holds the first drive open, so the second attempt has
    # to reach the runner slot and block on it while the first is in flight.
    tracking = _DriveTracking(released=threading.Event())
    run_a = RunId("lease-dispatch/concurrent-a")
    run_b = RunId("lease-dispatch/concurrent-b")
    runtime = _build_runtime(
        tmp_path, monkeypatch, tracking, (_lease_registration(True),)
    )
    try:
        _publish_catalog(runtime)
        runtime.launch()
        with caplog.at_level(logging.INFO, logger="atelier2"):
            _publish_and_start(
                runtime,
                run_a,
                "runner",
                _FREE_RUNNER_CONFIGURATION,
                _free_runner_document("first"),
            )
            _publish_and_start(
                runtime,
                run_b,
                "runner",
                _FREE_RUNNER_CONFIGURATION,
                _free_runner_document("second"),
            )
            # The proof: with one drive held open, the second attempt must log
            # that it is waiting on the runner slot before either can finish.
            # Deleting the slot lock lets both drives run at once, so this log
            # never appears and this wait fails -- `peak <= 1` alone would pass
            # vacuously if DBOS happened to run the two node workflows serially.
            wait_for_awaiting_runner_slot(caplog)
            tracking.released.set()
            wait_for_state(runtime, run_a, RunState.COMPLETED)
            wait_for_state(runtime, run_b, RunState.COMPLETED)
            wait_for_drives(tracking, 2)
        assert tracking.peak <= 1
    finally:
        tracking.released.set()
        runtime.close()


def test_refuse_unavailable_executor_holds_for_a_lease_carried_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`refuse_unavailable_executor` (`adapters/dbos/workflow.py`) is carrier-
    blind -- it fires once `executor is None`, before any carrier is ever
    consulted -- so this reaches it the same way
    `tests/integration/test_agent_configuration_v2.py::
    test_bound_unstarted_v2_run_fails_without_an_attempt_when_executor_is_unavailable`
    already proves for a `LOCAL_PROCESS` key, unaffected by C-3.6: bind a run
    to a `RUNNER_LEASE` key while it is startable, then restart the process
    with that same key registered unavailable, and confirm the durable node
    fails without ever reaching a driver.
    """

    run_id = RunId("lease-dispatch/unavailable-lease")
    seeded_tracking = _DriveTracking()
    seeded = _build_runtime(
        tmp_path, monkeypatch, seeded_tracking, (_lease_registration(True),)
    )
    try:
        _publish_catalog(seeded)
        _publish_and_start(
            seeded,
            run_id,
            "runner",
            _FREE_RUNNER_CONFIGURATION,
            _free_runner_document("refused"),
        )
    finally:
        seeded.close()

    restarted_tracking = _DriveTracking()
    restarted = _build_runtime(
        tmp_path, monkeypatch, restarted_tracking, (_lease_registration(False),)
    )
    try:
        restarted.launch()
        wait_for_state(restarted, run_id, RunState.FAILED)
        assert not restarted_tracking.completed_attempt_ids
    finally:
        restarted.close()
