from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DatabaseError

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.run_store import commit_subworkflow_completed
from atelier2.adapters.dbos.run_transitions import (
    RunTransitionConflict,
    run_from_record_with_bindings,
)
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import (
    agent_attempts,
    agent_receipts_v2,
    node_receipts_v3,
    run_events,
    runs,
)
from atelier2.adapters.dbos.starter import DbosDurableRunStarter
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.api.projection.events import run_event_resource
from atelier2.api.projection.runs import node_rail_resources
from atelier2.api.wire.events import (
    AgentFailedEventResourceV2,
    AgentFailedEventResourceV3,
)
from atelier2.application.converge_runner_terminal_evidence import (
    converge_runner_terminal_evidence,
)
from atelier2.application.project_node_rail import project_node_rail
from atelier2.contracts.agent_attempts import (
    AgentAttemptFailureCode,
    AgentAttemptId,
    AgentAttemptReplacement,
    AgentAttemptState,
    AgentProcessOwnerId,
    CancelAgentAttemptRequest,
    ProcessExitSignature,
    RunnerBindingConflict,
    RunnerCancellation,
    RunnerCancellationObservation,
    RunnerEvidenceAcceptancePhase,
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
    WatchdogGenerationId,
)
from atelier2.contracts.agents import (
    AgentExecutionRequestV2,
    AgentExecutionResult,
    AgentExecutorOperationalIdentity,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.executions import AgentAttemptExecution, NodeExecutionId
from atelier2.contracts.pages import PageLimit
from atelier2.contracts.run_bindings import RunV3
from atelier2.contracts.run_events import RunEventPage
from atelier2.contracts.run_projections import PublicAgentAttemptState
from atelier2.contracts.runs import RunId
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationAccepted,
    AgentAttemptCancellationTerminalConflict,
    AgentAttemptReplacementNotAllowed,
    RunnerTerminalEvidenceCommitRefused,
    RunnerTerminalEvidenceCommitted,
    RunnerTerminalEvidenceRefusal,
)
from atelier2.ports.durable_runs import DurableRunCreated, StartPublishedRunRequestV2
from atelier2.ports.run_queries import RunFound
from tests.integration.test_agent_attempts import attempt_request, attempt_runtime
from tests.integration.test_v3_agent_start import publish as publish_v3_workflow
from tests.integration.test_v3_tool_grant_run import (
    VERIFICATION_EXIT_CODE,
    granted_runtime,
    publish_granted_node,
)
from tests.scenarios.agents import (
    agent_attempt_execution,
    agent_scratch_root,
    failing_agent_executor_factory,
)
from tests.scenarios.api import durable_queries
from tests.scenarios.runners import FakeRunner


def _binding(
    execution: AgentAttemptExecution, generation: str = "runner-generation-1"
) -> RunnerGenerationBinding:
    return RunnerGenerationBinding(
        execution.attempt_id,
        execution.request.request_hash,
        RunnerGenerationId(generation),
        RunnerManifestId.of(b"runner-manifest-v1"),
    )


def _ack_tombstone(
    envelope: RunnerTerminalEvidenceEnvelope,
) -> RunnerTerminalEvidenceAckTombstone:
    return RunnerTerminalEvidenceAckTombstone(
        envelope.binding,
        envelope.invocation_id,
        RunnerTerminalEvidenceHash.for_envelope(envelope),
    )


def _bound(
    root: Path,
    run_name: str,
    generation: str = "runner-generation-1",
) -> tuple[
    DbosRuntime,
    DbosAgentAttemptStore,
    AgentAttemptExecution,
    RunnerGenerationBinding,
]:
    runtime = attempt_runtime(root)
    runtime.initialize_storage()
    request = attempt_request(runtime, run_name)
    execution = agent_attempt_execution(request)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)
    store.prepare(execution)
    binding = _binding(execution, generation)
    store.bind_runner_generation(execution, binding)
    return runtime, store, execution, binding


def _armed(
    root: Path, run_name: str
) -> tuple[
    DbosRuntime,
    DbosAgentAttemptStore,
    AgentAttemptExecution,
    RunnerGenerationBinding,
    RunnerInvocationId,
]:
    runtime, store, execution, binding = _bound(root, run_name)
    invocation = RunnerInvocationId("runner-invocation-1")
    store.arm_runner_invocation(execution, binding, invocation)
    return runtime, store, execution, binding, invocation


_DEFAULT_V3_RUNNER_INVOCATION = RunnerInvocationId("runner-invocation-1")


def _v3_armed(
    root: Path,
    run_name: str,
    invocation: RunnerInvocationId = _DEFAULT_V3_RUNNER_INVOCATION,
) -> tuple[
    DbosRuntime,
    DbosAgentAttemptStore,
    AgentAttemptExecution,
    RunnerGenerationBinding,
    RunnerInvocationId,
]:
    runtime = DbosRuntime(
        DbosRuntimeSettings(
            root / "atelier.sqlite",
            "runner-evidence-v3-test",
            agent_scratch_root=agent_scratch_root(root),
        ),
        LoopbackEffectAdapterFactory(
            root / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        ExactOutputAgentExecutorFactory(),
        (failing_agent_executor_factory("exact", []),),
    )
    runtime.initialize_storage()
    workflow, bindings = publish_v3_workflow(runtime)
    run_id = RunId(run_name)
    started = DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        runtime.agent_executor_registry,
        effect_adapter_proves_absence=True,
    ).start_published(
        StartPublishedRunRequestV2(run_id, workflow.revision_hash, bindings)
    )
    assert isinstance(started, DurableRunCreated)
    with runtime.engine.connect() as connection:
        record = (
            connection.execute(sa.select(runs).where(runs.c.run_id == run_id.value))
            .mappings()
            .one()
        )
        run = run_from_record_with_bindings(connection, record)
    assert isinstance(run, RunV3)
    request = AgentExecutionRequestV2(
        NodeExecutionId.for_node(run_id, workflow.revision_hash, "implement"),
        run_id,
        workflow.revision_hash,
        "implement",
        run.agent_bindings[0],
        AgentExecutorOperationalIdentity("exact-operation"),
        b"Do the one thing this chain is for.",
    )
    execution = AgentAttemptExecution(
        request,
        AgentAttemptId.for_execution(request.node_execution_id, request.request_hash),
        1,
    )
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)
    store.prepare(execution)
    binding = _binding(execution)
    store.bind_runner_generation(execution, binding)
    store.arm_runner_invocation(execution, binding, invocation)
    return runtime, store, execution, binding, invocation


def _runner_product_snapshot(
    runtime: DbosRuntime,
) -> tuple[tuple[tuple[object, ...], ...], ...]:
    with runtime.engine.connect() as connection:
        return tuple(
            tuple(
                tuple(row)
                for row in connection.execute(
                    sa.select(table).order_by(*table.primary_key.columns)
                )
            )
            for table in (
                runs,
                agent_attempts,
                agent_receipts_v2,
                node_receipts_v3,
                run_events,
            )
        )


def test_runner_binding_and_arm_are_idempotent_and_collision_closed(
    tmp_path: Path,
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    execution = agent_attempt_execution(
        attempt_request(runtime, "runner/evidence/bind-arm")
    )
    store = DbosAgentAttemptStore(runtime.engine)
    store.prepare(execution)
    binding = _binding(execution)
    try:
        bound = store.bind_runner_generation(execution, binding)
        assert store.bind_runner_generation(execution, binding) == bound
        with pytest.raises(RunnerBindingConflict):
            store.bind_runner_generation(
                execution, _binding(execution, "another-generation")
            )

        invocation = RunnerInvocationId("runner-invocation-1")
        armed = store.arm_runner_invocation(execution, binding, invocation)
        assert store.arm_runner_invocation(execution, binding, invocation) == armed
        with pytest.raises(RunnerBindingConflict):
            store.arm_runner_invocation(
                execution, binding, RunnerInvocationId("another-invocation")
            )
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "acceptance_phase",
    (
        RunnerEvidenceAcceptancePhase.CORE_COMMITTED,
        RunnerEvidenceAcceptancePhase.ACKNOWLEDGED,
    ),
)
def test_delayed_bind_and_arm_retries_confirm_monotonic_runner_facts_without_write(
    tmp_path: Path, acceptance_phase: RunnerEvidenceAcceptancePhase
) -> None:
    runtime, store, execution, binding, invocation = _armed(
        tmp_path, f"runner/evidence/delayed-{acceptance_phase.value.lower()}"
    )
    try:
        envelope = RunnerTerminalEvidenceEnvelope(
            binding, invocation, RunnerProcessBoundaryFailure()
        )
        store.commit_runner_terminal_evidence(execution, envelope)
        if acceptance_phase is RunnerEvidenceAcceptancePhase.ACKNOWLEDGED:
            store.mark_runner_evidence_acknowledged(execution, _ack_tombstone(envelope))
        before = store.load(execution.attempt_id)
        snapshot = _runner_product_snapshot(runtime)

        assert store.bind_runner_generation(execution, binding) == before
        assert store.arm_runner_invocation(execution, binding, invocation) == before

        changed_manifest = RunnerGenerationBinding(
            binding.attempt_id,
            binding.request_hash,
            binding.generation_id,
            RunnerManifestId.of(b"another-manifest"),
        )
        changed_generation = RunnerGenerationBinding(
            binding.attempt_id,
            binding.request_hash,
            RunnerGenerationId("another-generation"),
            binding.manifest_id,
        )
        with pytest.raises(RunnerBindingConflict):
            store.bind_runner_generation(execution, changed_manifest)
        with pytest.raises(RunnerBindingConflict):
            store.bind_runner_generation(execution, changed_generation)
        with pytest.raises(RunnerBindingConflict):
            store.arm_runner_invocation(execution, changed_manifest, invocation)
        with pytest.raises(RunnerBindingConflict):
            store.arm_runner_invocation(execution, changed_generation, invocation)
        with pytest.raises(RunnerBindingConflict):
            store.arm_runner_invocation(
                execution, binding, RunnerInvocationId("another-invocation")
            )
        assert _runner_product_snapshot(runtime) == snapshot
    finally:
        runtime.close()


def test_runner_evidence_commits_product_truth_once(tmp_path: Path) -> None:
    runtime, store, execution, binding, invocation = _armed(
        tmp_path, "runner/evidence/success"
    )
    try:
        envelope = RunnerTerminalEvidenceEnvelope(
            binding,
            invocation,
            RunnerProviderResult(AgentExecutionResult(b"answer")),
        )

        first = store.commit_runner_terminal_evidence(execution, envelope)
        retry = store.commit_runner_terminal_evidence(execution, envelope)

        assert isinstance(first, RunnerTerminalEvidenceCommitted)
        assert isinstance(retry, RunnerTerminalEvidenceCommitted)
        assert retry.evidence_hash == first.evidence_hash
        assert retry.attempt == first.attempt
        assert first.attempt.state is AgentAttemptState.SUCCEEDED
        assert (
            first.attempt.runner_evidence_acceptance_phase
            is RunnerEvidenceAcceptancePhase.CORE_COMMITTED
        )
        with runtime.engine.connect() as connection:
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(agent_receipts_v2)
                )
                == 1
            )
            assert (
                connection.scalar(sa.select(sa.func.count()).select_from(run_events))
                == 1
            )
    finally:
        runtime.close()


def test_exact_evidence_retry_converges_after_legitimate_downstream_progress(
    tmp_path: Path,
) -> None:
    runtime, store, execution, binding, invocation = _armed(
        tmp_path, "runner/evidence/downstream-progress"
    )
    try:
        envelope = RunnerTerminalEvidenceEnvelope(
            binding,
            invocation,
            RunnerProviderResult(AgentExecutionResult(b"answer in hand")),
        )
        committed = store.commit_runner_terminal_evidence(execution, envelope)
        assert isinstance(committed, RunnerTerminalEvidenceCommitted)
        with canonical_write_transaction(runtime.engine) as connection:
            commit_subworkflow_completed(
                connection,
                execution.request.run_id,
                execution.request.workflow_revision_hash,
                "done",
                5,
            )
        snapshot = _runner_product_snapshot(runtime)

        assert store.commit_runner_terminal_evidence(execution, envelope) == committed

        with pytest.raises(RunnerBindingConflict):
            store.commit_runner_terminal_evidence(
                execution,
                RunnerTerminalEvidenceEnvelope(
                    binding,
                    invocation,
                    RunnerProviderResult(AgentExecutionResult(b"different answer")),
                ),
            )
        changed_generation = RunnerGenerationBinding(
            binding.attempt_id,
            binding.request_hash,
            RunnerGenerationId("another-generation"),
            binding.manifest_id,
        )
        changed_manifest = RunnerGenerationBinding(
            binding.attempt_id,
            binding.request_hash,
            binding.generation_id,
            RunnerManifestId.of(b"another-manifest"),
        )
        with pytest.raises(RunnerBindingConflict):
            store.commit_runner_terminal_evidence(
                execution,
                RunnerTerminalEvidenceEnvelope(
                    changed_generation,
                    invocation,
                    RunnerProviderResult(AgentExecutionResult(b"answer in hand")),
                ),
            )
        with pytest.raises(RunnerBindingConflict):
            store.commit_runner_terminal_evidence(
                execution,
                RunnerTerminalEvidenceEnvelope(
                    changed_manifest,
                    invocation,
                    RunnerProviderResult(AgentExecutionResult(b"answer in hand")),
                ),
            )
        assert _runner_product_snapshot(runtime) == snapshot
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("evidence", "failure", "reason"),
    (
        (
            RunnerProviderFailure(ProcessExitSignature(17, b"provider died")),
            AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY,
            "process-exited-unsuccessfully",
        ),
        (
            RunnerOutputLimitExceeded(
                frozenset(
                    {
                        RunnerOutputStream.STANDARD_OUTPUT,
                        RunnerOutputStream.STANDARD_ERROR,
                    }
                )
            ),
            AgentAttemptFailureCode.PROCESS_OUTPUT_LIMIT_EXCEEDED,
            "process-output-limit-exceeded: STANDARD_ERROR, STANDARD_OUTPUT",
        ),
        (
            RunnerProcessBoundaryFailure(),
            AgentAttemptFailureCode.PROCESS_SUPERVISION_FAILED,
            "process-supervision-failed",
        ),
    ),
)
@pytest.mark.parametrize("cancel_first", (False, True))
def test_typed_runner_failures_reach_one_named_product_seam(
    tmp_path: Path,
    evidence: RunnerTerminalEvidence,
    failure: AgentAttemptFailureCode,
    reason: str,
    cancel_first: bool,
) -> None:
    runtime, store, execution, binding, invocation = _armed(
        tmp_path, f"runner/evidence/{failure.value.lower()}"
    )
    try:
        if cancel_first:
            durable = store.load(execution.attempt_id)
            accepted = store.request_cancellation(
                CancelAgentAttemptRequest(
                    execution.request.run_id,
                    execution.attempt_id,
                    "operator-cancel",
                    durable.state_version,
                    AgentAttemptReplacement.NONE,
                )
            )
            assert isinstance(accepted, AgentAttemptCancellationAccepted)
        committed = store.commit_runner_terminal_evidence(
            execution, RunnerTerminalEvidenceEnvelope(binding, invocation, evidence)
        )

        assert isinstance(committed, RunnerTerminalEvidenceCommitted)
        assert committed.attempt.state is AgentAttemptState.FAILED
        assert committed.attempt.failure_code is failure
        assert committed.attempt.cancellation is None
        found = durable_queries(runtime.engine).get_run(execution.request.run_id)
        assert isinstance(found, RunFound)
        assert found.projection.current_agent_attempt is not None
        assert found.projection.current_agent_attempt.failure_code is failure
        with runtime.engine.connect() as connection:
            events = tuple(
                connection.execute(
                    sa.select(run_events).order_by(run_events.c.event_sequence)
                ).mappings()
            )
            event = events[-1]
            assert [str(record["event_kind"]) for record in events] == (
                ["AGENT_CANCEL_REQUESTED", "AGENT_FAILED"]
                if cancel_first
                else ["AGENT_FAILED"]
            )
            assert event["payload"] == failure.value.encode("ascii")
            stored_reason = connection.scalar(
                sa.text(
                    "SELECT reason FROM node_receipts_v3 "
                    "WHERE node_execution_id=:execution"
                ),
                {"execution": execution.request.node_execution_id.value},
            )
            assert stored_reason is None or stored_reason == reason
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("evidence", "failure_code"),
    (
        (
            RunnerOutputLimitExceeded(frozenset({RunnerOutputStream.STANDARD_OUTPUT})),
            AgentAttemptFailureCode.PROCESS_OUTPUT_LIMIT_EXCEEDED,
        ),
        (
            RunnerProcessBoundaryFailure(),
            AgentAttemptFailureCode.PROCESS_SUPERVISION_FAILED,
        ),
    ),
)
def test_real_v2_runner_failures_project_the_exact_new_event_code(
    tmp_path: Path,
    evidence: RunnerTerminalEvidence,
    failure_code: AgentAttemptFailureCode,
) -> None:
    runtime, store, execution, binding, invocation = _armed(
        tmp_path, f"runner/evidence/v2-wire-{failure_code.value.lower()}"
    )
    try:
        store.commit_runner_terminal_evidence(
            execution, RunnerTerminalEvidenceEnvelope(binding, invocation, evidence)
        )
        queries = durable_queries(runtime.engine)
        page = queries.read_run_event_page(execution.request.run_id, 0, 5)
        found = queries.get_run(execution.request.run_id)
        assert isinstance(page, RunEventPage)
        assert isinstance(found, RunFound)
        assert len(page.events) == 1

        resource = run_event_resource(
            page.events[0],
            node_rail_resources(project_node_rail(found.projection, page.events)),
        )

        assert isinstance(resource, AgentFailedEventResourceV2)
        assert resource.failure_code == failure_code.value
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("evidence", "failure_code", "receipt_reason"),
    (
        (
            RunnerOutputLimitExceeded(frozenset({RunnerOutputStream.STANDARD_OUTPUT})),
            AgentAttemptFailureCode.PROCESS_OUTPUT_LIMIT_EXCEEDED,
            "process-output-limit-exceeded: STANDARD_OUTPUT",
        ),
        (
            RunnerProcessBoundaryFailure(),
            AgentAttemptFailureCode.PROCESS_SUPERVISION_FAILED,
            "process-supervision-failed",
        ),
    ),
)
def test_real_grantless_v3_runner_failures_keep_and_project_the_exact_new_reason(
    tmp_path: Path,
    evidence: RunnerTerminalEvidence,
    failure_code: AgentAttemptFailureCode,
    receipt_reason: str,
) -> None:
    runtime, store, execution, binding, invocation = _v3_armed(
        tmp_path, f"runner/evidence/v3-wire-{failure_code.value.lower()}"
    )
    try:
        committed = store.commit_runner_terminal_evidence(
            execution, RunnerTerminalEvidenceEnvelope(binding, invocation, evidence)
        )
        assert isinstance(committed, RunnerTerminalEvidenceCommitted)
        assert committed.attempt.failure_code is failure_code
        queries = durable_queries(runtime.engine)
        page = queries.read_run_event_page(execution.request.run_id, 0, 5)
        found = queries.get_run(execution.request.run_id)
        assert isinstance(page, RunEventPage)
        assert isinstance(found, RunFound)
        assert len(page.events) == 1
        assert page.events[0].node_receipt_reason == receipt_reason
        assert found.projection.current_agent_attempt is not None
        assert found.projection.current_agent_attempt.failure_code is failure_code
        with runtime.engine.connect() as connection:
            assert connection.execute(
                sa.select(
                    node_receipts_v3.c.disposition,
                    node_receipts_v3.c.reason,
                )
            ).one() == ("failed", receipt_reason)
            assert connection.execute(
                sa.select(run_events.c.event_kind, run_events.c.payload)
            ).one() == ("AGENT_FAILED", failure_code.value.encode("ascii"))

        resource = run_event_resource(
            page.events[0],
            node_rail_resources(project_node_rail(found.projection, page.events)),
        )

        assert isinstance(resource, AgentFailedEventResourceV3)
        assert resource.failure_code == failure_code.value
        assert resource.reason == receipt_reason
    finally:
        runtime.close()


@pytest.mark.parametrize("with_invocation", (False, True))
def test_acked_never_launched_is_the_only_pre_arm_rebind(
    tmp_path: Path, with_invocation: bool
) -> None:
    runtime, store, execution, binding = _bound(
        tmp_path, f"runner/no-launch/{with_invocation}"
    )
    try:
        invocation = (
            RunnerInvocationId("accepted-but-not-armed") if with_invocation else None
        )
        envelope = RunnerTerminalEvidenceEnvelope(
            binding,
            invocation,
            RunnerCancellation(
                "cancel-before-arm", RunnerCancellationObservation.NEVER_LAUNCHED
            ),
        )
        committed = store.commit_runner_terminal_evidence(execution, envelope)
        assert isinstance(committed, RunnerTerminalEvidenceCommitted)
        assert committed.attempt.state is AgentAttemptState.PREPARED
        assert committed.attempt.cancellation is None
        if invocation is not None:
            with pytest.raises(RunnerBindingConflict):
                store.arm_runner_invocation(execution, binding, invocation)
        with runtime.engine.connect() as connection:
            assert (
                connection.scalar(sa.select(sa.func.count()).select_from(run_events))
                == 0
            )
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(agent_receipts_v2)
                )
                == 0
            )

        tombstone = _ack_tombstone(envelope)
        fresh = RunnerGenerationBinding(
            execution.attempt_id,
            execution.request.request_hash,
            RunnerGenerationId("runner-generation-2"),
            RunnerManifestId.of(b"runner-manifest-v2"),
        )
        before_early_rebind = _runner_product_snapshot(runtime)
        with pytest.raises(RunnerBindingConflict):
            store.rebind_after_acknowledged_never_launched(execution, tombstone, fresh)
        assert _runner_product_snapshot(runtime) == before_early_rebind

        acknowledged = store.mark_runner_evidence_acknowledged(execution, tombstone)
        assert (
            store.mark_runner_evidence_acknowledged(execution, tombstone)
            == acknowledged
        )
        if invocation is not None:
            acknowledged_snapshot = _runner_product_snapshot(runtime)
            with pytest.raises(RunnerBindingConflict):
                store.arm_runner_invocation(execution, binding, invocation)
            assert _runner_product_snapshot(runtime) == acknowledged_snapshot
        acknowledged_snapshot = _runner_product_snapshot(runtime)
        for drifted in (
            replace(
                tombstone,
                binding=replace(
                    binding, manifest_id=RunnerManifestId.of(b"foreign-manifest")
                ),
            ),
            replace(tombstone, invocation_id=RunnerInvocationId("foreign-invocation")),
            replace(
                tombstone,
                evidence_hash=RunnerTerminalEvidenceHash.of(b"foreign-evidence"),
            ),
        ):
            with pytest.raises(RunnerBindingConflict):
                store.rebind_after_acknowledged_never_launched(
                    execution, drifted, fresh
                )
        assert _runner_product_snapshot(runtime) == acknowledged_snapshot
        rebound = store.rebind_after_acknowledged_never_launched(
            execution, tombstone, fresh
        )
        retry = store.rebind_after_acknowledged_never_launched(
            execution, tombstone, fresh
        )

        assert (
            acknowledged.runner_evidence_acceptance_phase
            is RunnerEvidenceAcceptancePhase.ACKNOWLEDGED
        )
        assert rebound.runner_generation_id == fresh.generation_id
        assert rebound.runner_manifest_id == fresh.manifest_id
        assert rebound.runner_invocation_id is None
        assert rebound.runner_terminal_evidence_hash is None
        assert (
            rebound.runner_evidence_acceptance_phase
            is RunnerEvidenceAcceptancePhase.NONE
        )
        assert retry == rebound
        retry_snapshot = _runner_product_snapshot(runtime)
        with pytest.raises(RunnerBindingConflict):
            store.rebind_after_acknowledged_never_launched(
                execution,
                tombstone,
                RunnerGenerationBinding(
                    execution.attempt_id,
                    execution.request.request_hash,
                    fresh.generation_id,
                    RunnerManifestId.of(b"runner-manifest-v3"),
                ),
            )
        with pytest.raises(RunnerBindingConflict):
            store.rebind_after_acknowledged_never_launched(
                execution,
                tombstone,
                RunnerGenerationBinding(
                    execution.attempt_id,
                    execution.request.request_hash,
                    RunnerGenerationId("runner-generation-3"),
                    fresh.manifest_id,
                ),
            )
        assert _runner_product_snapshot(runtime) == retry_snapshot
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "target_state", ("armed", "committed", "acknowledged", "succeeded")
)
def test_rebind_retry_refuses_a_fresh_target_that_has_already_moved(
    tmp_path: Path, target_state: str
) -> None:
    runtime, store, execution, binding = _bound(
        tmp_path, f"runner/no-launch/retry-{target_state}"
    )
    try:
        old_evidence = RunnerTerminalEvidenceEnvelope(
            binding,
            None,
            RunnerCancellation(
                "cancel-before-arm", RunnerCancellationObservation.NEVER_LAUNCHED
            ),
        )
        store.commit_runner_terminal_evidence(execution, old_evidence)
        old_tombstone = _ack_tombstone(old_evidence)
        store.mark_runner_evidence_acknowledged(execution, old_tombstone)
        fresh = RunnerGenerationBinding(
            execution.attempt_id,
            execution.request.request_hash,
            RunnerGenerationId("runner-generation-2"),
            RunnerManifestId.of(b"runner-manifest-v2"),
        )
        store.rebind_after_acknowledged_never_launched(execution, old_tombstone, fresh)

        if target_state in {"armed", "succeeded"}:
            store.arm_runner_invocation(
                execution, fresh, RunnerInvocationId("runner-invocation-2")
            )
        if target_state == "succeeded":
            store.commit_runner_terminal_evidence(
                execution,
                RunnerTerminalEvidenceEnvelope(
                    fresh,
                    RunnerInvocationId("runner-invocation-2"),
                    RunnerProviderResult(AgentExecutionResult(b"terminal product")),
                ),
            )
        elif target_state != "armed":
            fresh_evidence = RunnerTerminalEvidenceEnvelope(
                fresh,
                None,
                RunnerCancellation(
                    "second-cancel-before-arm",
                    RunnerCancellationObservation.NEVER_LAUNCHED,
                ),
            )
            store.commit_runner_terminal_evidence(execution, fresh_evidence)
            if target_state == "acknowledged":
                store.mark_runner_evidence_acknowledged(
                    execution, _ack_tombstone(fresh_evidence)
                )
        before = store.load(execution.attempt_id)

        with pytest.raises(RunnerBindingConflict):
            store.rebind_after_acknowledged_never_launched(
                execution, old_tombstone, fresh
            )
        assert store.load(execution.attempt_id) == before
    finally:
        runtime.close()


def test_runner_invocation_loss_remains_possibly_ran_without_product_drift(
    tmp_path: Path,
) -> None:
    runtime, store, execution, binding, invocation = _armed(
        tmp_path, "runner/evidence/lost"
    )
    try:
        committed = store.commit_runner_terminal_evidence(
            execution,
            RunnerTerminalEvidenceEnvelope(binding, invocation, RunnerInvocationLost()),
        )

        assert isinstance(committed, RunnerTerminalEvidenceCommitted)
        assert committed.attempt.state is AgentAttemptState.LAUNCH_ARMED
        assert tuple(store.iter_driverless_attempts(PageLimit(1))) == ()
        found = durable_queries(runtime.engine).get_run(execution.request.run_id)
        assert isinstance(found, RunFound)
        assert found.projection.current_agent_attempt is not None
        assert (
            found.projection.current_agent_attempt.state
            is PublicAgentAttemptState.POSSIBLY_RAN
        )
        with runtime.engine.connect() as connection:
            assert (
                connection.scalar(sa.select(sa.func.count()).select_from(run_events))
                == 0
            )
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(agent_receipts_v2)
                )
                == 0
            )
    finally:
        runtime.close()


def test_runner_completion_wins_cancel_transaction_order(tmp_path: Path) -> None:
    runtime, store, execution, binding, invocation = _armed(
        tmp_path, "runner/evidence/cancel-first"
    )
    try:
        command = CancelAgentAttemptRequest(
            execution.request.run_id,
            execution.attempt_id,
            "operator-cancel",
            store.load(execution.attempt_id).state_version,
            AgentAttemptReplacement.NONE,
        )
        accepted = store.request_cancellation(command)
        assert isinstance(accepted, AgentAttemptCancellationAccepted)

        committed = store.commit_runner_terminal_evidence(
            execution,
            RunnerTerminalEvidenceEnvelope(
                binding,
                invocation,
                RunnerProviderResult(AgentExecutionResult(b"finished in hand")),
            ),
        )

        assert isinstance(committed, RunnerTerminalEvidenceCommitted)
        assert committed.attempt.state is AgentAttemptState.SUCCEEDED
        assert committed.attempt.cancellation is None
        with runtime.engine.connect() as connection:
            assert (
                connection.scalar(sa.select(agent_receipts_v2.c.output_bytes))
                == b"finished in hand"
            )
            assert tuple(
                connection.execute(
                    sa.select(run_events.c.event_kind, run_events.c.payload).order_by(
                        run_events.c.event_sequence
                    )
                )
            ) == (
                ("AGENT_CANCEL_REQUESTED", b"operator-cancel"),
                ("AGENT_COMPLETED", b"finished in hand"),
            )

        # Success already won the race and cleared the cancellation columns
        # above; a crossing CANCEL that still reaches the store afterward
        # (the wire-level race `test_runner_session_wire.py` proves) finds no
        # pending command left to repeat and must not be silently accepted.
        repeated = store.request_cancellation(command)
        assert isinstance(repeated, AgentAttemptCancellationTerminalConflict)
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "observation",
    (
        RunnerCancellationObservation.EXITED_BEFORE_SIGNAL,
        RunnerCancellationObservation.REAPED_AFTER_TERM,
        RunnerCancellationObservation.REAPED_AFTER_KILL,
    ),
)
def test_runner_cancellation_maps_only_the_exact_core_command(
    tmp_path: Path, observation: RunnerCancellationObservation
) -> None:
    runtime, store, execution, binding, invocation = _armed(
        tmp_path, "runner/evidence/cancelled"
    )
    try:
        command = CancelAgentAttemptRequest(
            execution.request.run_id,
            execution.attempt_id,
            "operator-cancel",
            store.load(execution.attempt_id).state_version,
            AgentAttemptReplacement.NONE,
        )
        assert isinstance(
            store.request_cancellation(command), AgentAttemptCancellationAccepted
        )
        committed = store.commit_runner_terminal_evidence(
            execution,
            RunnerTerminalEvidenceEnvelope(
                binding,
                invocation,
                RunnerCancellation(command.command_id, observation),
            ),
        )

        assert isinstance(committed, RunnerTerminalEvidenceCommitted)
        assert committed.attempt.state is AgentAttemptState.CANCELLED
        assert committed.attempt.cancellation is not None
        assert committed.attempt.cancellation.disposition is not None
        assert committed.attempt.cancellation.disposition.value == observation.value
    finally:
        runtime.close()


def test_runner_cancellation_winner_rejects_late_provider_bytes_without_drift(
    tmp_path: Path,
) -> None:
    runtime, store, execution, binding, invocation = _armed(
        tmp_path, "runner/evidence/cancellation-wins"
    )
    try:
        command = CancelAgentAttemptRequest(
            execution.request.run_id,
            execution.attempt_id,
            "operator-cancel",
            store.load(execution.attempt_id).state_version,
            AgentAttemptReplacement.NONE,
        )
        assert isinstance(
            store.request_cancellation(command), AgentAttemptCancellationAccepted
        )
        cancelled = store.commit_runner_terminal_evidence(
            execution,
            RunnerTerminalEvidenceEnvelope(
                binding,
                invocation,
                RunnerCancellation(
                    command.command_id,
                    RunnerCancellationObservation.REAPED_AFTER_TERM,
                ),
            ),
        )
        assert isinstance(cancelled, RunnerTerminalEvidenceCommitted)
        assert cancelled.attempt.state is AgentAttemptState.CANCELLED
        snapshot = _runner_product_snapshot(runtime)

        with pytest.raises(RunnerBindingConflict):
            store.commit_runner_terminal_evidence(
                execution,
                RunnerTerminalEvidenceEnvelope(
                    binding,
                    invocation,
                    RunnerProviderResult(
                        AgentExecutionResult(b"too late to become product truth")
                    ),
                ),
            )

        assert store.load(execution.attempt_id) == cancelled.attempt
        assert _runner_product_snapshot(runtime) == snapshot

        # Cancel won the race; the same command retried afterward -- exactly
        # what a crossing CANCEL that still reaches the store produces (see
        # `test_runner_session_wire.py`) -- must recognize its own already
        # terminal command rather than either re-applying it or refusing it.
        repeated = store.request_cancellation(command)
        assert isinstance(repeated, AgentAttemptCancellationAccepted)
        assert repeated.terminal is True
        assert repeated.attempt == cancelled.attempt
    finally:
        runtime.close()


def test_runner_cancellation_for_another_command_is_not_product_evidence(
    tmp_path: Path,
) -> None:
    runtime, store, execution, binding, invocation = _armed(
        tmp_path, "runner/evidence/wrong-cancel-command"
    )
    try:
        before_cancel = store.load(execution.attempt_id)
        accepted = store.request_cancellation(
            CancelAgentAttemptRequest(
                execution.request.run_id,
                execution.attempt_id,
                "core-command",
                before_cancel.state_version,
                AgentAttemptReplacement.NONE,
            )
        )
        assert isinstance(accepted, AgentAttemptCancellationAccepted)
        with pytest.raises(RunnerBindingConflict):
            store.commit_runner_terminal_evidence(
                execution,
                RunnerTerminalEvidenceEnvelope(
                    binding,
                    invocation,
                    RunnerCancellation(
                        "another-command",
                        RunnerCancellationObservation.REAPED_AFTER_TERM,
                    ),
                ),
            )

        durable = store.load(execution.attempt_id)
        assert durable.state is AgentAttemptState.CANCEL_REQUESTED
        assert durable.runner_terminal_evidence_hash is None
        with runtime.engine.connect() as connection:
            assert tuple(
                connection.scalars(
                    sa.select(run_events.c.event_kind).order_by(
                        run_events.c.event_sequence
                    )
                )
            ) == ("AGENT_CANCEL_REQUESTED",)
    finally:
        runtime.close()


def test_runner_replacement_is_closed_before_state_event_or_row_drift(
    tmp_path: Path,
) -> None:
    runtime, store, execution, binding, invocation = _armed(
        tmp_path, "runner/evidence/replacement-refused"
    )
    try:
        committed = store.commit_runner_terminal_evidence(
            execution,
            RunnerTerminalEvidenceEnvelope(binding, invocation, RunnerInvocationLost()),
        )
        assert isinstance(committed, RunnerTerminalEvidenceCommitted)
        before = store.load(execution.attempt_id)
        assert before.runner_terminal_evidence_hash is not None
        assert (
            before.runner_evidence_acceptance_phase
            is RunnerEvidenceAcceptancePhase.CORE_COMMITTED
        )
        refused = store.request_cancellation(
            CancelAgentAttemptRequest(
                execution.request.run_id,
                execution.attempt_id,
                "replace",
                before.state_version,
                AgentAttemptReplacement.ONE,
            )
        )

        assert isinstance(refused, AgentAttemptReplacementNotAllowed)
        assert store.load(execution.attempt_id) == before
        with runtime.engine.connect() as connection:
            assert (
                connection.scalar(sa.select(sa.func.count()).select_from(run_events))
                == 0
            )
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(agent_receipts_v2)
                )
                == 0
            )
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(node_receipts_v3)
                )
                == 0
            )
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(agent_attempts)
                )
                == 1
            )
        assert tuple(store.iter_driverless_attempts(PageLimit(1))) == ()
    finally:
        runtime.close()


def test_runner_binding_is_closed_to_every_legacy_execution_path(
    tmp_path: Path,
) -> None:
    runtime, store, execution, _binding_value = _bound(
        tmp_path, "runner/evidence/no-legacy-owner"
    )
    try:
        before = store.load(execution.attempt_id)
        with pytest.raises(RunTransitionConflict):
            store.bind_watchdog(
                execution,
                AgentProcessOwnerId("legacy-owner"),
                WatchdogGenerationId("legacy-generation"),
            )
        with pytest.raises(RunTransitionConflict):
            store.claim(execution)

        assert store.load(execution.attempt_id) == before
        assert tuple(store.iter_driverless_attempts(PageLimit(1))) == ()
    finally:
        runtime.close()


def test_grant_bound_runner_evidence_is_refused_before_any_commit(
    tmp_path: Path,
) -> None:
    lifecycle = granted_runtime(tmp_path, VERIFICATION_EXIT_CODE)
    runtime, _scratch_root, _cwd_record = next(lifecycle)
    try:
        workflow, bindings, _grant_revision = publish_granted_node(runtime)
        run_id = RunId("runner/evidence/grant-refused")
        started = DbosDurableRunStarter(
            runtime.engine,
            runtime.settings,
            runtime.agent_executor_registry,
            effect_adapter_proves_absence=True,
        ).start_published(
            StartPublishedRunRequestV2(run_id, workflow.revision_hash, bindings)
        )
        assert isinstance(started, DurableRunCreated)
        with runtime.engine.connect() as connection:
            record = (
                connection.execute(sa.select(runs).where(runs.c.run_id == run_id.value))
                .mappings()
                .one()
            )
            run = run_from_record_with_bindings(connection, record)
        assert isinstance(run, RunV3)
        resolved = run.agent_bindings[0]
        request = AgentExecutionRequestV2(
            NodeExecutionId.for_node(run_id, workflow.revision_hash, "implement"),
            run_id,
            workflow.revision_hash,
            "implement",
            resolved,
            AgentExecutorOperationalIdentity("exact-operation"),
            b"Do the one thing this chain is for.",
        )
        execution = AgentAttemptExecution(
            request,
            AgentAttemptId.for_execution(
                request.node_execution_id, request.request_hash
            ),
            1,
        )
        store = DbosAgentAttemptStore(runtime.engine)
        store.prepare(execution)
        binding = _binding(execution)
        store.bind_runner_generation(execution, binding)
        runner = FakeRunner()
        invocation = runner.accept(binding)
        store.arm_runner_invocation(execution, binding, invocation)
        before = store.load(execution.attempt_id)
        envelope = runner.observe(
            RunnerTerminalEvidenceEnvelope(
                binding,
                invocation,
                RunnerProviderResult(AgentExecutionResult(b"held by Runner")),
            )
        )

        refused = converge_runner_terminal_evidence(execution, binding, runner, store)

        assert refused == RunnerTerminalEvidenceCommitRefused(
            RunnerTerminalEvidenceRefusal.TOOL_GRANT_BOUND
        )
        assert store.load(execution.attempt_id) == before
        assert runner.observed_evidence(binding) == envelope
        assert runner.acknowledgement_count(binding) == 0
        assert runner.garbage_collection_count(binding) == 0
        with runtime.engine.connect() as connection:
            assert (
                connection.scalar(sa.select(sa.func.count()).select_from(run_events))
                == 0
            )
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(agent_receipts_v2)
                )
                == 0
            )
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(node_receipts_v3)
                )
                == 0
            )
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(agent_attempts)
                )
                == 1
            )
    finally:
        with pytest.raises(StopIteration):
            next(lifecycle)


def test_different_evidence_collides_while_same_evidence_is_idempotent(
    tmp_path: Path,
) -> None:
    runtime, store, execution, binding, invocation = _armed(
        tmp_path, "runner/evidence/race"
    )
    try:
        envelopes = (
            RunnerTerminalEvidenceEnvelope(
                binding, invocation, RunnerProviderResult(AgentExecutionResult(b"one"))
            ),
            RunnerTerminalEvidenceEnvelope(
                binding, invocation, RunnerProviderResult(AgentExecutionResult(b"two"))
            ),
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(store.commit_runner_terminal_evidence, execution, envelope)
                for envelope in envelopes
            ]
        answers: list[object] = []
        for future in futures:
            try:
                answers.append(future.result(timeout=5))
            except RunnerBindingConflict as error:
                answers.append(error)

        assert (
            sum(
                isinstance(answer, RunnerTerminalEvidenceCommitted)
                for answer in answers
            )
            == 1
        )
        assert sum(isinstance(answer, RunnerBindingConflict) for answer in answers) == 1
    finally:
        runtime.close()


def test_same_evidence_race_commits_one_product_result(tmp_path: Path) -> None:
    runtime, store, execution, binding, invocation = _armed(
        tmp_path, "runner/evidence/same-race"
    )
    try:
        envelope = RunnerTerminalEvidenceEnvelope(
            binding,
            invocation,
            RunnerProviderResult(AgentExecutionResult(b"one durable answer")),
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = tuple(
                pool.submit(store.commit_runner_terminal_evidence, execution, envelope)
                for _ in range(2)
            )
        answers = tuple(future.result(timeout=5) for future in futures)

        assert all(
            isinstance(answer, RunnerTerminalEvidenceCommitted) for answer in answers
        )
        assert answers[0] == answers[1]
        with runtime.engine.connect() as connection:
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(agent_receipts_v2)
                )
                == 1
            )
            assert (
                connection.scalar(sa.select(sa.func.count()).select_from(run_events))
                == 1
            )
    finally:
        runtime.close()


def test_ack_requires_the_exact_committed_evidence(tmp_path: Path) -> None:
    runtime, store, execution, binding, invocation = _armed(
        tmp_path, "runner/evidence/ack-order"
    )
    try:
        envelope = RunnerTerminalEvidenceEnvelope(
            binding, invocation, RunnerInvocationLost()
        )
        with pytest.raises(RunnerBindingConflict):
            store.mark_runner_evidence_acknowledged(execution, _ack_tombstone(envelope))

        store.commit_runner_terminal_evidence(execution, envelope)
        different = RunnerTerminalEvidenceEnvelope(
            binding,
            invocation,
            RunnerProviderFailure(ProcessExitSignature(1, b"different")),
        )
        with pytest.raises(RunnerBindingConflict):
            store.mark_runner_evidence_acknowledged(
                execution, _ack_tombstone(different)
            )
        assert (
            store.load(execution.attempt_id).runner_evidence_acceptance_phase
            is RunnerEvidenceAcceptancePhase.CORE_COMMITTED
        )
    finally:
        runtime.close()


def test_product_and_evidence_rollback_together_before_event_commit(
    tmp_path: Path,
) -> None:
    runtime, store, execution, binding, invocation = _armed(
        tmp_path, "runner/evidence/rollback"
    )
    try:
        with runtime.engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TRIGGER refuse_runner_event BEFORE INSERT ON run_events "
                "BEGIN SELECT RAISE(ABORT, 'cut before event'); END"
            )
        with pytest.raises(DatabaseError, match="cut before event"):
            store.commit_runner_terminal_evidence(
                execution,
                RunnerTerminalEvidenceEnvelope(
                    binding,
                    invocation,
                    RunnerProviderResult(AgentExecutionResult(b"answer")),
                ),
            )

        durable = store.load(execution.attempt_id)
        assert durable.state is AgentAttemptState.LAUNCH_ARMED
        assert durable.runner_terminal_evidence_hash is None
        assert (
            durable.runner_evidence_acceptance_phase
            is RunnerEvidenceAcceptancePhase.NONE
        )
        with runtime.engine.connect() as connection:
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(agent_receipts_v2)
                )
                == 0
            )
            assert (
                connection.scalar(sa.select(sa.func.count()).select_from(run_events))
                == 0
            )
    finally:
        runtime.close()
