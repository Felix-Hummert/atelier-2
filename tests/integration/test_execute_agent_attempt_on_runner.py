"""The Core-side runner-lease driver (`#540` C-3.4), against real production
parts wherever one exists: a real `DbosAgentAttemptStore`, a real
`FileRunnerLeasePublisher` on disk, a real `CoreRunnerSession` state machine
driven with real wire bytes. Only the socket/TLS transport is a scripted
in-process stand-in (`tests.scenarios.runners.drive_free_runner_session_to_released`)
-- the real transport and wire protocol are already pinned end to end by
`tests/integration/test_runner_session_wire.py`; this module's subject is the
driver's own composition around an already-accepted session, not the
transport underneath it.
"""

from __future__ import annotations

import inspect
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from atelier2.adapters.dbos.runner_session_core import DbosRunnerSessionCore
from atelier2.adapters.file_runner_leases import FileRunnerLeasePublisher
from atelier2.adapters.free_runner_executor import FreeRunnerPrintJob
from atelier2.adapters.runner_cli_pins import runner_executor_cli_pin
from atelier2.application import execute_agent_attempt_on_runner as driver_module
from atelier2.application.execute_agent_attempt_on_runner import (
    RunnerAttemptLeaseMaterial,
    execute_agent_attempt_on_runner,
)
from atelier2.contracts.agent_attempts import (
    AgentAttemptState,
    RunnerGenerationBinding,
    RunnerInvocationId,
    RunnerTerminalEvidenceEnvelope,
)
from atelier2.contracts.runner_leases import RunnerLeaseId
from atelier2.contracts.runner_manifests import RunnerManifestV1, runner_manifest_id
from atelier2.ports.agent_attempts import (
    AgentAttemptSucceeded,
    RunnerTerminalEvidenceCommitted,
)
from atelier2.ports.durable_runs import DurableWriteUnavailable
from atelier2.ports.runner_leases import (
    PublishRunnerLeaseResult,
    RunnerInvocationTimedOut,
    RunnerLeaseExisting,
    RunnerLeasePublished,
    RunnerLeaseRequest,
    RunnerPeerMaterial,
)
from tests.scenarios.runners import (
    RunnerSessionAdvancerLike,
    drive_free_runner_session_to_released,
    free_runner_candidate_manifest,
    prepared_free_runner_attempt,
)

_RUNNER_IMAGE = "atelier2-runner-candidate:test"
_SERVE_CONTAINER = "serve-test"
_CA_CERTIFICATE = b"scenario-ca-placeholder"
_CORE_CERTIFICATE = b"scenario-core-certificate-placeholder"
_CORE_PEER_DOCUMENT = b"scenario-core-peer-document-placeholder"
_PEER_MATERIAL_TIMEOUT_SECONDS = 5.0


def _material(
    manifest: RunnerManifestV1, auth_reference: str
) -> RunnerAttemptLeaseMaterial:
    return RunnerAttemptLeaseMaterial(
        manifest,
        _RUNNER_IMAGE,
        _SERVE_CONTAINER,
        _CA_CERTIFICATE,
        _CORE_CERTIFICATE,
        _CORE_PEER_DOCUMENT,
        auth_reference,
        runner_executor_cli_pin(manifest),
    )


@dataclass
class _ScriptedTransport:
    """A fake `RunnerLeaseSessionTransport`: instead of a real socket/TLS
    accept, it builds the session through the driver's own factory and plays
    the Runner side against it directly
    (`drive_free_runner_session_to_released`)."""

    invocation_id: RunnerInvocationId
    manifest: RunnerManifestV1
    auth_reference: str
    output_bytes: bytes
    resend_as_tombstone: bool = False
    drive_calls: int = 0
    envelope: RunnerTerminalEvidenceEnvelope | None = field(default=None, init=False)

    def drive_to_released(
        self,
        binding: RunnerGenerationBinding,
        peer: RunnerPeerMaterial,
        session_for_first_connection: Callable[
            [RunnerInvocationId], RunnerSessionAdvancerLike
        ],
        on_started: object | None = None,
    ) -> None:
        del peer, on_started
        self.drive_calls += 1
        session = session_for_first_connection(self.invocation_id)
        self.envelope = drive_free_runner_session_to_released(
            session,
            binding,
            self.invocation_id,
            self.manifest,
            self.auth_reference,
            self.output_bytes,
            resend_as_tombstone=self.resend_as_tombstone,
        )


class _UnreachableTransport:
    """A `RunnerLeaseSessionTransport` a driver call must never reach when no
    Runner ever connects -- calling it at all fails the test loudly."""

    def drive_to_released(self, *args: object, **kwargs: object) -> None:
        raise AssertionError(
            "the session transport must not be driven when the invocation "
            "channel never produced peer material"
        )


def _spawn_peer_material_writer(
    attempt_root: Path, lease_id: RunnerLeaseId, manifest_id_value: str
) -> tuple[threading.Thread, list[BaseException]]:
    """A stand-in for the launcher's own handoff: once this driver's
    `leases.publish` has made the Attempt's directories exist, write the two
    files `serve_one_invocation` waits on -- the peer leaf (any bytes; the
    scripted transport never parses them) and the inspect attestation (must
    name the exact manifest id this driver bound)."""

    errors: list[BaseException] = []

    def _write() -> None:
        try:
            attempt_directory = attempt_root / lease_id.value
            peer_directory = attempt_directory / "peer"
            handoff_directory = attempt_directory / "handoff"
            deadline = time.monotonic() + _PEER_MATERIAL_TIMEOUT_SECONDS
            while not (peer_directory.is_dir() and handoff_directory.is_dir()):
                if time.monotonic() > deadline:
                    raise TimeoutError("scenario lease material was never published")
                time.sleep(0.01)
            (peer_directory / "client.crt").write_bytes(
                b"scenario-peer-leaf-placeholder"
            )
            (handoff_directory / "inspect-attested").write_text(
                manifest_id_value, encoding="ascii"
            )
        except BaseException as error:  # noqa: BLE001 -- surfaced to the test thread
            errors.append(error)

    thread = threading.Thread(target=_write)
    thread.start()
    return thread, errors


def test_released_translates_into_the_stores_own_durable_completion(
    tmp_path: Path,
) -> None:
    """RELEASED ends this driver in `AgentAttemptSucceeded` carrying the exact
    `NodeCompletion` the durable store itself computed -- not a value this
    driver invents, proven by asking the store to recompute it independently
    on its own idempotent retry of the exact same evidence."""
    prepared = prepared_free_runner_attempt(
        tmp_path, "runner/on-runner/success", FreeRunnerPrintJob("hello from runner")
    )
    try:
        manifest = free_runner_candidate_manifest()
        material = _material(manifest, prepared.auth_reference)
        leases = FileRunnerLeasePublisher(tmp_path / "leases", tmp_path / "attempts")
        core = DbosRunnerSessionCore(
            prepared.execution, prepared.store, "runner-attempt-cancel"
        )
        invocation_id = RunnerInvocationId("B" * 43)
        transport = _ScriptedTransport(
            invocation_id, manifest, prepared.auth_reference, b'"runner said hello"'
        )
        lease_id = RunnerLeaseId(prepared.execution.attempt_id.value)
        writer, errors = _spawn_peer_material_writer(
            tmp_path / "attempts", lease_id, runner_manifest_id(manifest).value
        )
        try:
            outcome = execute_agent_attempt_on_runner(
                prepared.execution,
                prepared.store,
                prepared.store,
                core,
                transport,
                leases,
                leases,
                material,
                invocation_deadline_seconds=_PEER_MATERIAL_TIMEOUT_SECONDS,
            )
        finally:
            writer.join(timeout=_PEER_MATERIAL_TIMEOUT_SECONDS)
        assert not errors

        assert isinstance(outcome, AgentAttemptSucceeded)
        assert outcome.attempt.state is AgentAttemptState.SUCCEEDED
        assert transport.drive_calls == 1
        assert transport.envelope is not None

        retry = prepared.store.commit_runner_terminal_evidence(
            prepared.execution, transport.envelope
        )
        assert isinstance(retry, RunnerTerminalEvidenceCommitted)
        assert retry.completion == outcome.completion
        assert outcome.completion is not None
    finally:
        prepared.runtime.close()


def test_replay_reuses_the_durable_generation_and_publishes_no_second_lease(
    tmp_path: Path,
) -> None:
    """A second call for the same execution -- the shape a durable-workflow
    recovery replay takes -- reuses the durable `RunnerGenerationId` instead
    of minting a second one (which the store would refuse as a
    `RunnerBindingConflict`), and its lease publish converges on the same
    document rather than a second lease for the one Attempt its
    `RunnerLeaseId` already names."""
    prepared = prepared_free_runner_attempt(
        tmp_path, "runner/on-runner/replay", FreeRunnerPrintJob("hello from runner")
    )
    try:
        manifest = free_runner_candidate_manifest()
        material = _material(manifest, prepared.auth_reference)
        published_results: list[PublishRunnerLeaseResult] = []
        published_generation_ids: list[str] = []
        real_leases = FileRunnerLeasePublisher(
            tmp_path / "leases", tmp_path / "attempts"
        )

        class _CountingLeasePublisher:
            def publish(self, request: RunnerLeaseRequest) -> PublishRunnerLeaseResult:
                published_generation_ids.append(request.binding.generation_id.value)
                result = real_leases.publish(request)
                published_results.append(result)
                return result

            def withdraw(self, lease_id: RunnerLeaseId):
                return real_leases.withdraw(lease_id)

            def serve_one_invocation(
                self, lease_id: RunnerLeaseId, deadline_seconds: float
            ):
                return real_leases.serve_one_invocation(lease_id, deadline_seconds)

        leases = _CountingLeasePublisher()
        invocation_id = RunnerInvocationId("C" * 43)
        lease_id = RunnerLeaseId(prepared.execution.attempt_id.value)
        writer, errors = _spawn_peer_material_writer(
            tmp_path / "attempts", lease_id, runner_manifest_id(manifest).value
        )
        try:
            first = execute_agent_attempt_on_runner(
                prepared.execution,
                prepared.store,
                prepared.store,
                DbosRunnerSessionCore(
                    prepared.execution, prepared.store, "runner-attempt-cancel-1"
                ),
                _ScriptedTransport(
                    invocation_id, manifest, prepared.auth_reference, b'"same evidence"'
                ),
                leases,
                leases,
                material,
                invocation_deadline_seconds=_PEER_MATERIAL_TIMEOUT_SECONDS,
            )
        finally:
            writer.join(timeout=_PEER_MATERIAL_TIMEOUT_SECONDS)
        assert not errors
        assert isinstance(first, AgentAttemptSucceeded)

        second = execute_agent_attempt_on_runner(
            prepared.execution,
            prepared.store,
            prepared.store,
            DbosRunnerSessionCore(
                prepared.execution, prepared.store, "runner-attempt-cancel-2"
            ),
            _ScriptedTransport(
                invocation_id, manifest, prepared.auth_reference, b'"same evidence"'
            ),
            leases,
            leases,
            material,
            invocation_deadline_seconds=_PEER_MATERIAL_TIMEOUT_SECONDS,
        )

        assert isinstance(second, AgentAttemptSucceeded)
        assert second.completion == first.completion
        assert published_generation_ids[0] == published_generation_ids[1]
        assert [type(result) for result in published_results] == [
            RunnerLeasePublished,
            RunnerLeaseExisting,
        ]
        assert (
            prepared.store.load(prepared.execution.attempt_id).runner_generation_id
            is not None
        )
    finally:
        prepared.runtime.close()


def test_a_resumed_tombstone_replay_still_translates_into_the_durable_outcome(
    tmp_path: Path,
) -> None:
    """A fresh driver call for the same execution -- a Core/Serve crash
    followed by DBOS recovery re-invoking `execute_agent_attempt_on_runner`
    with fresh `store`/`core` instances -- whose reconnecting Runner presents
    only its already-tombstoned journal (it received Core's ACK before this
    exact Core process died, the scenario `DbosRunnerSessionCore`'s own
    `_require_already_acknowledged` docstring names) must still translate
    RELEASED into the durable `AgentAttemptSucceeded`, not an unhandled
    `AssertionError` for a `committed_evidence` this path used to leave
    unset."""
    prepared = prepared_free_runner_attempt(
        tmp_path,
        "runner/on-runner/tombstone-resume",
        FreeRunnerPrintJob("hello from runner"),
    )
    try:
        manifest = free_runner_candidate_manifest()
        material = _material(manifest, prepared.auth_reference)
        leases = FileRunnerLeasePublisher(tmp_path / "leases", tmp_path / "attempts")
        invocation_id = RunnerInvocationId("D" * 43)
        lease_id = RunnerLeaseId(prepared.execution.attempt_id.value)
        writer, errors = _spawn_peer_material_writer(
            tmp_path / "attempts", lease_id, runner_manifest_id(manifest).value
        )
        try:
            first = execute_agent_attempt_on_runner(
                prepared.execution,
                prepared.store,
                prepared.store,
                DbosRunnerSessionCore(
                    prepared.execution,
                    prepared.store,
                    "runner-attempt-cancel-1",
                    engine=prepared.runtime.engine,
                ),
                _ScriptedTransport(
                    invocation_id,
                    manifest,
                    prepared.auth_reference,
                    b'"resume evidence"',
                ),
                leases,
                leases,
                material,
                invocation_deadline_seconds=_PEER_MATERIAL_TIMEOUT_SECONDS,
            )
        finally:
            writer.join(timeout=_PEER_MATERIAL_TIMEOUT_SECONDS)
        assert not errors
        assert isinstance(first, AgentAttemptSucceeded)

        second = execute_agent_attempt_on_runner(
            prepared.execution,
            prepared.store,
            prepared.store,
            DbosRunnerSessionCore(
                prepared.execution,
                prepared.store,
                "runner-attempt-cancel-2",
                engine=prepared.runtime.engine,
            ),
            _ScriptedTransport(
                invocation_id,
                manifest,
                prepared.auth_reference,
                b'"resume evidence"',
                resend_as_tombstone=True,
            ),
            leases,
            leases,
            material,
            invocation_deadline_seconds=_PEER_MATERIAL_TIMEOUT_SECONDS,
        )

        assert isinstance(second, AgentAttemptSucceeded)
        assert second.completion == first.completion
    finally:
        prepared.runtime.close()


def test_a_runner_that_never_connects_invents_no_evidence_and_withdraws_its_lease(
    tmp_path: Path,
) -> None:
    """A launcher that never establishes this Attempt's Runner within the
    invocation deadline leaves this driver with nothing to commit: it must
    not fabricate terminal evidence, and it must physically withdraw the
    lease so no stray material outlives the Attempt it named."""
    prepared = prepared_free_runner_attempt(
        tmp_path, "runner/on-runner/never-connects", FreeRunnerPrintJob("unused")
    )
    try:
        manifest = free_runner_candidate_manifest()
        material = _material(manifest, prepared.auth_reference)
        attempt_root = tmp_path / "attempts"
        lease_directory = tmp_path / "leases"
        leases = FileRunnerLeasePublisher(lease_directory, attempt_root)
        core = DbosRunnerSessionCore(
            prepared.execution, prepared.store, "runner-attempt-cancel"
        )
        lease_id = RunnerLeaseId(prepared.execution.attempt_id.value)

        outcome = execute_agent_attempt_on_runner(
            prepared.execution,
            prepared.store,
            prepared.store,
            core,
            _UnreachableTransport(),
            leases,
            leases,
            material,
            invocation_deadline_seconds=0.2,
        )

        assert outcome == RunnerInvocationTimedOut(lease_id)
        durable = prepared.store.load(prepared.execution.attempt_id)
        assert durable.state is AgentAttemptState.PREPARED
        assert durable.runner_generation_id is not None
        assert durable.runner_terminal_evidence_hash is None
        assert not (lease_directory / "open" / f"{lease_id.value}.json").exists()
        assert (lease_directory / "withdrawn" / f"{lease_id.value}.json").is_file()
        assert not (attempt_root / lease_id.value).exists()
    finally:
        prepared.runtime.close()


def test_the_driver_never_names_the_legacy_process_execution_ports() -> None:
    """`#540` C-3.4's own acceptance line: no call in this module ever
    touches `AgentSession` or `AgentAttemptWorkspaceOwner` -- the local-
    process path's own carrier ports, which this Runner-lease path replaces
    rather than reuses."""
    source = inspect.getsource(driver_module)
    assert "AgentSession" not in source
    assert "AgentAttemptWorkspaceOwner" not in source


def test_a_publish_failure_is_named_rather_than_silently_ignored(
    tmp_path: Path,
) -> None:
    prepared = prepared_free_runner_attempt(
        tmp_path, "runner/on-runner/publish-unavailable", FreeRunnerPrintJob("unused")
    )
    try:
        manifest = free_runner_candidate_manifest()
        material = _material(manifest, prepared.auth_reference)

        class _RefusingPublisher:
            def publish(self, request: RunnerLeaseRequest) -> PublishRunnerLeaseResult:
                del request
                return DurableWriteUnavailable()

            def withdraw(self, lease_id: RunnerLeaseId):
                raise AssertionError("withdraw must not be reached")

            def serve_one_invocation(
                self, lease_id: RunnerLeaseId, deadline_seconds: float
            ):
                raise AssertionError("serve_one_invocation must not be reached")

        with pytest.raises(driver_module.RunnerAttemptLeasePublishUnavailable):
            execute_agent_attempt_on_runner(
                prepared.execution,
                prepared.store,
                prepared.store,
                DbosRunnerSessionCore(
                    prepared.execution, prepared.store, "runner-attempt-cancel"
                ),
                _UnreachableTransport(),
                _RefusingPublisher(),
                _RefusingPublisher(),
                material,
                invocation_deadline_seconds=0.2,
            )
    finally:
        prepared.runtime.close()
