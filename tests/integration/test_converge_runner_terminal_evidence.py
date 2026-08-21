from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.application.converge_runner_terminal_evidence import (
    converge_runner_terminal_evidence,
)
from atelier2.contracts.agent_attempts import (
    AgentAttempt,
    AgentAttemptFailureCode,
    RunnerBindingConflict,
    RunnerCancellation,
    RunnerCancellationObservation,
    RunnerEvidenceAcceptancePhase,
    RunnerGenerationBinding,
    RunnerGenerationId,
    RunnerInvocationId,
    RunnerInvocationLost,
    RunnerManifestId,
    RunnerProviderResult,
    RunnerTerminalEvidenceAckTombstone,
    RunnerTerminalEvidenceEnvelope,
)
from atelier2.contracts.agents import AgentExecutionResult
from atelier2.contracts.executions import AgentAttemptExecution
from atelier2.contracts.runner_terminal_evidence_codec import (
    RunnerTerminalEvidenceRecordCorrupt,
    RunnerTerminalEvidenceRecordMissing,
    RunnerTerminalEvidenceRecordOversized,
    encode_runner_terminal_evidence_record,
)
from atelier2.ports.agent_attempts import (
    RunnerTerminalEvidenceAcknowledgementUnavailable,
    RunnerTerminalEvidenceCommitResult,
)
from tests.integration.test_runner_terminal_evidence_store import _bound, _v3_armed
from tests.scenarios.runners import FakeRunner, SimulatedRunnerCrash


class _CrashCutStore(DbosAgentAttemptStore):
    def __init__(self, engine: Engine, cut: str) -> None:
        super().__init__(engine)
        self._cut = cut

    def commit_runner_terminal_evidence(
        self,
        execution: AgentAttemptExecution,
        envelope: RunnerTerminalEvidenceEnvelope,
    ) -> RunnerTerminalEvidenceCommitResult:
        if self._cut == "before-commit":
            raise SimulatedRunnerCrash("after readback before Core commit")
        result = super().commit_runner_terminal_evidence(execution, envelope)
        if self._cut == "after-commit":
            raise SimulatedRunnerCrash("after Core commit before ACK")
        return result


def _armed_invocation_loss(
    root: Path,
    run_name: str,
    generation: str = "runner-generation-1",
    runner: FakeRunner | None = None,
) -> tuple[
    DbosRuntime,
    DbosAgentAttemptStore,
    AgentAttemptExecution,
    RunnerGenerationBinding,
    FakeRunner,
]:
    runtime, store, execution, binding = _bound(root, run_name, generation)
    source = FakeRunner() if runner is None else runner
    invocation = source.accept(binding)
    store.arm_runner_invocation(execution, binding, invocation)
    source.launch(binding, invocation)
    source.observe(
        RunnerTerminalEvidenceEnvelope(binding, invocation, RunnerInvocationLost())
    )
    return runtime, store, execution, binding, source


@pytest.mark.parametrize(
    ("record", "outcome"),
    (
        (None, RunnerTerminalEvidenceRecordMissing()),
        (b"corrupt", RunnerTerminalEvidenceRecordCorrupt()),
        (b"x" * 57_762, RunnerTerminalEvidenceRecordOversized()),
    ),
    ids=("missing", "corrupt", "oversized"),
)
def test_unusable_runner_records_preserve_core_and_runner_without_acknowledging(
    tmp_path: Path,
    record: bytes | None,
    outcome: object,
) -> None:
    runtime, store, execution, binding = _bound(
        tmp_path, f"runner/evidence/{type(outcome).__name__}"
    )
    runner = FakeRunner()
    runner.accept(binding)
    runner.retain_record(binding, record)
    before = store.load(execution.attempt_id)
    try:
        assert (
            converge_runner_terminal_evidence(execution, binding, runner, store)
            == outcome
        )
        assert store.load(execution.attempt_id) == before
        assert runner.record_bytes(binding) == record
        assert runner.acknowledgement_count(binding) == 0
        assert runner.garbage_collection_count(binding) == 0
    finally:
        runtime.close()


def test_acknowledgement_unavailable_keeps_committed_core_and_envelope_for_retry(
    tmp_path: Path,
) -> None:
    runtime, store, execution, binding, runner = _armed_invocation_loss(
        tmp_path, "runner/evidence/ack-unavailable"
    )
    envelope_bytes = runner.record_bytes(binding)
    runner.fail_next_acknowledgement(binding)
    try:
        assert (
            converge_runner_terminal_evidence(execution, binding, runner, store)
            == RunnerTerminalEvidenceAcknowledgementUnavailable()
        )
        committed = store.load(execution.attempt_id)
        assert committed.runner_evidence_acceptance_phase is (
            RunnerEvidenceAcceptancePhase.CORE_COMMITTED
        )
        assert runner.record_bytes(binding) == envelope_bytes
        assert runner.garbage_collection_count(binding) == 0

        converged = converge_runner_terminal_evidence(execution, binding, runner, store)
        assert isinstance(converged, AgentAttempt)
        assert converged.runner_evidence_acceptance_phase is (
            RunnerEvidenceAcceptancePhase.ACKNOWLEDGED
        )
        assert runner.garbage_collection_count(binding) == 1
    finally:
        runtime.close()


def test_valid_record_from_another_generation_conflicts_without_core_mutation(
    tmp_path: Path,
) -> None:
    runtime, store, execution, binding, runner = _armed_invocation_loss(
        tmp_path, "runner/evidence/foreign-source-binding"
    )
    foreign = replace(binding, generation_id=RunnerGenerationId("foreign-generation"))
    record = encode_runner_terminal_evidence_record(
        RunnerTerminalEvidenceEnvelope(
            foreign,
            RunnerInvocationId("runner-invocation-runner-generation-1"),
            RunnerInvocationLost(),
        )
    )
    runner.retain_record(binding, record)
    before = store.load(execution.attempt_id)
    try:
        with pytest.raises(RunnerBindingConflict, match="requested binding"):
            converge_runner_terminal_evidence(execution, binding, runner, store)

        assert store.load(execution.attempt_id) == before
        assert runner.record_bytes(binding) == record
        assert runner.acknowledgement_count(binding) == 0
    finally:
        runtime.close()


def test_valid_provider_bytes_refused_by_product_schema_are_still_acknowledged(
    tmp_path: Path,
) -> None:
    invocation = RunnerInvocationId("runner-invocation-runner-generation-1")
    runtime, store, execution, binding, invocation = _v3_armed(
        tmp_path, "runner/evidence/schema-refused", invocation
    )
    runner = FakeRunner()
    assert runner.accept(binding) == invocation
    runner.observe(
        RunnerTerminalEvidenceEnvelope(
            binding,
            invocation,
            RunnerProviderResult(AgentExecutionResult(b"not-json")),
        )
    )
    try:
        converged = converge_runner_terminal_evidence(execution, binding, runner, store)

        assert isinstance(converged, AgentAttempt)
        assert converged.failure_code is AgentAttemptFailureCode.OUTPUT_SCHEMA_REFUSED
        assert converged.runner_evidence_acceptance_phase is (
            RunnerEvidenceAcceptancePhase.ACKNOWLEDGED
        )
        assert runner.garbage_collection_count(binding) == 1
        assert b"not-json" not in (runner.record_bytes(binding) or b"")
    finally:
        runtime.close()


@pytest.mark.parametrize("crash_after_ack", (False, True))
def test_ack_gc_tombstone_marks_core_and_survives_a_crash_before_rebind(
    tmp_path: Path, crash_after_ack: bool
) -> None:
    runtime, store, execution, binding = _bound(
        tmp_path, "runner/evidence/crash-after-ack"
    )
    runner = FakeRunner()
    runner.accept(binding)
    runner.observe(
        RunnerTerminalEvidenceEnvelope(
            binding,
            None,
            RunnerCancellation(
                "cancel-before-arm", RunnerCancellationObservation.NEVER_LAUNCHED
            ),
        )
    )
    if crash_after_ack:
        runner.fail_after_next_acknowledge(binding)
    try:
        if crash_after_ack:
            with pytest.raises(
                SimulatedRunnerCrash, match="after terminal-evidence ACK"
            ):
                converge_runner_terminal_evidence(execution, binding, runner, store)
            durable = store.load(execution.attempt_id)
            assert durable.runner_evidence_acceptance_phase is (
                RunnerEvidenceAcceptancePhase.CORE_COMMITTED
            )
        else:
            converge_runner_terminal_evidence(execution, binding, runner, store)
        tombstone = runner.readback(binding)
        assert isinstance(tombstone, RunnerTerminalEvidenceAckTombstone)
        assert runner.observed_evidence(binding) is None

        acknowledged = converge_runner_terminal_evidence(
            execution, binding, runner, store
        )
        assert isinstance(acknowledged, AgentAttempt)
        assert (
            acknowledged.runner_evidence_acceptance_phase
            is RunnerEvidenceAcceptancePhase.ACKNOWLEDGED
        )
        fresh = RunnerGenerationBinding(
            execution.attempt_id,
            execution.request.request_hash,
            RunnerGenerationId("runner-generation-2"),
            RunnerManifestId.of(b"runner-manifest-v2"),
        )
        rebound = store.rebind_after_acknowledged_never_launched(
            execution, tombstone, fresh
        )

        assert rebound.runner_generation_id == fresh.generation_id
        assert runner.acknowledgement_count(binding) == 1
        assert runner.garbage_collection_count(binding) == 1
    finally:
        runtime.close()


@pytest.mark.parametrize("cut", ("before-readback", "before-commit", "after-commit"))
def test_every_pre_ack_crash_cut_retries_the_same_invocation_without_gc(
    tmp_path: Path, cut: str
) -> None:
    runtime, store, execution, binding, runner = _armed_invocation_loss(
        tmp_path, f"runner/evidence/{cut}"
    )
    if cut == "before-readback":
        runner.fail_next_readback(binding)
        cut_store = store
    else:
        cut_store = _CrashCutStore(runtime.engine, cut)
    try:
        with pytest.raises(SimulatedRunnerCrash):
            converge_runner_terminal_evidence(execution, binding, runner, cut_store)

        durable = store.load(execution.attempt_id)
        assert durable.runner_evidence_acceptance_phase is (
            RunnerEvidenceAcceptancePhase.CORE_COMMITTED
            if cut == "after-commit"
            else RunnerEvidenceAcceptancePhase.NONE
        )
        assert runner.acknowledgement_count(binding) == 0
        assert runner.garbage_collection_count(binding) == 0

        converged = converge_runner_terminal_evidence(execution, binding, runner, store)
        assert isinstance(converged, AgentAttempt)
        assert converged.runner_evidence_acceptance_phase is (
            RunnerEvidenceAcceptancePhase.ACKNOWLEDGED
        )
        assert runner.provider_start_count == 1
        assert runner.garbage_collection_count(binding) == 1
    finally:
        runtime.close()


def test_two_consumers_converge_one_generation_and_collect_once(tmp_path: Path) -> None:
    runtime, store, execution, binding, runner = _armed_invocation_loss(
        tmp_path, "runner/evidence/two-consumers"
    )
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = tuple(
                pool.submit(
                    converge_runner_terminal_evidence,
                    execution,
                    binding,
                    runner,
                    store,
                )
                for _ in range(2)
            )
            answers = tuple(future.result(timeout=5) for future in futures)

        assert all(isinstance(answer, AgentAttempt) for answer in answers)
        assert answers[0] == answers[1]
        assert (
            converge_runner_terminal_evidence(execution, binding, runner, store)
            == answers[0]
        )
        assert runner.garbage_collection_count(binding) == 1
    finally:
        runtime.close()


def test_one_blocked_generation_does_not_serialize_another(tmp_path: Path) -> None:
    first = _armed_invocation_loss(
        tmp_path / "first", "runner/evidence/generation-one", "generation-one"
    )
    first_runtime, first_store, first_execution, first_binding, first_runner = first
    first_runtime.close()
    second = _armed_invocation_loss(
        tmp_path / "second",
        "runner/evidence/generation-two",
        "generation-two",
        first_runner,
    )
    second_runtime, second_store, second_execution, second_binding, second_runner = (
        second
    )
    entered, release = threading.Event(), threading.Event()
    first_runner.hold_acknowledge(first_binding, entered, release)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            blocked = pool.submit(
                converge_runner_terminal_evidence,
                first_execution,
                first_binding,
                first_runner,
                first_store,
            )
            assert entered.wait(timeout=5)
            independent = pool.submit(
                converge_runner_terminal_evidence,
                second_execution,
                second_binding,
                second_runner,
                second_store,
            )
            assert isinstance(independent.result(timeout=5), AgentAttempt)
            release.set()
            assert isinstance(blocked.result(timeout=5), AgentAttempt)
    finally:
        release.set()
        second_runtime.close()


def test_runner_io_can_take_an_independent_sqlite_write_transaction(
    tmp_path: Path,
) -> None:
    runtime, store, execution, binding, runner = _armed_invocation_loss(
        tmp_path, "runner/evidence/sqlite-outside"
    )
    with runtime.engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE probe (step TEXT PRIMARY KEY)")

    def write_probe(step: str) -> None:
        with canonical_write_transaction(runtime.engine) as connection:
            connection.exec_driver_sql("INSERT INTO probe (step) VALUES (?)", (step,))

    runner.probe_calls(
        binding,
        lambda: write_probe("readback"),
        lambda: write_probe("acknowledge"),
    )
    try:
        converge_runner_terminal_evidence(execution, binding, runner, store)
        with runtime.engine.connect() as connection:
            steps = set(connection.scalars(sa.text("SELECT step FROM probe")))
            assert steps == {"readback", "acknowledge"}
    finally:
        runtime.close()
