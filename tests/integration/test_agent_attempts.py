from __future__ import annotations

import ast
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DatabaseError, IntegrityError

import atelier2.adapters.dbos.agent_attempt_store as agent_attempt_store_module
import atelier2.adapters.dbos.run_store as run_store_module
from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.run_store import RunTransitionConflict
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import (
    agent_attempts,
    agent_receipts_v2,
    run_events,
    runs,
)
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.application.execute_agent_attempt import execute_agent_attempt
from atelier2.contracts.agent_attempts import (
    AgentAttemptFailureCode,
    driving_workflow_id,
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
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.run_bindings import RunV2
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
from atelier2.contracts.workflows import (
    AgentNodeV2,
    RunCompletes,
    RunContinues,
    WorkflowGraphV2,
)
from atelier2.ports.agent_attempts import (
    AgentAttemptClaimedByThisCall,
    AgentAttemptFailed,
    AgentAttemptPossiblyRan,
    AgentAttemptSucceeded,
)
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AgentConfigurationRevisionExisting,
    AuthProfileRevisionCreated,
    AuthProfileRevisionExisting,
)
from atelier2.ports.agent_executions import (
    AgentExecutionFailure,
    AgentProcessCompletion,
)
from atelier2.ports.durable_runs import DurableRunCreated, StartPublishedRunRequestV2
from atelier2.ports.run_queries import RunFound
from atelier2.ports.workflow_revisions import QueryDurableStateCorrupt
from tests.scenarios.agents import (
    AgentCompletionDecoder,
    RecordingAgentExecutorFactoryV2,
    RecordingAgentExecutorV2,
    agent_attempt_execution,
    agent_scratch_root,
    answering,
    decode_process_exit,
    emitting,
    launching,
    runtime_workspace_owner,
)
from tests.scenarios.api import durable_queries

_DOCUMENT = b"""format_version: 2
start: build
nodes:
  - {id: done, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: build, type: agent, role: builder, job: build, next: done}
"""


def attempt_runtime(
    root: Path, *, agent_process_cgroup_root: Path | None = None
) -> DbosRuntime:
    return DbosRuntime(
        DbosRuntimeSettings(
            root / "atelier.sqlite",
            "attempt-test",
            agent_process_cgroup_root=agent_process_cgroup_root,
            agent_scratch_root=agent_scratch_root(root),
        ),
        LoopbackEffectAdapterFactory(
            root / "effects.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("test"),
        ),
        ExactOutputAgentExecutorFactory(),
        (
            RecordingAgentExecutorFactoryV2(
                "anthropic", "claude-cli/v1", "controlled-process", b"unused"
            ),
        ),
    )


def attempt_request(
    runtime: DbosRuntime, run_name: str = "attempt/run"
) -> AgentExecutionRequestV2:
    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    auth = AuthProfileRevision("max", 1, ProviderId("anthropic"), AuthMode.SUBSCRIPTION)
    assert isinstance(
        catalog.publish_auth_profile_revision(auth),
        (AuthProfileRevisionCreated, AuthProfileRevisionExisting),
    )
    configuration = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision("claude-cli/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    assert isinstance(
        catalog.publish_agent_configuration_revision(configuration),
        (AgentConfigurationRevisionCreated, AgentConfigurationRevisionExisting),
    )
    workflow = WorkflowRevision(_DOCUMENT)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    run_id = RunId(run_name)
    started = DbosDurableRunStarter(
        runtime.engine, runtime.settings, runtime.agent_executor_registry
    ).start_published(
        StartPublishedRunRequestV2(
            run_id,
            workflow.revision_hash,
            AgentBindingSet(
                (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
            ),
        )
    )
    assert isinstance(started, DurableRunCreated)
    assert isinstance(started.run, RunV2)
    resolved = started.run.agent_bindings[0]
    return AgentExecutionRequestV2(
        NodeExecutionId.for_node(run_id, workflow.revision_hash, "build"),
        run_id,
        workflow.revision_hash,
        "build",
        resolved,
        AgentExecutorOperationalIdentity("controlled-process"),
        b"build",
    )


def _record_driving_workflow(
    runtime: DbosRuntime, workflow_id: str, status: str
) -> None:
    """Leave one DBOS workflow row in the state a real one would be found in.

    The durable runtime owns this table; a test that wants to ask about a
    workflow in a status only a crash or a raise produces cannot reach that
    status by running one.
    """

    with runtime.engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO workflow_status "
                "(workflow_uuid, status, created_at, updated_at, priority) "
                "VALUES (:workflow_id, :status, 0, 0, 0)"
            ),
            {"workflow_id": workflow_id, "status": status},
        )


def _observing_durable_process_phase(runtime: DbosRuntime) -> AgentCompletionDecoder:
    """A decoder reading back what the durable attempt says while it decodes."""

    def decode(
        completion: AgentProcessCompletion,
    ) -> AgentExecutionResult | AgentExecutionFailure:
        with runtime.engine.connect() as connection:
            state = connection.scalar(sa.select(agent_attempts.c.process_phase))
        assert state == "PROCESS_OBSERVED"
        return decode_process_exit(completion)

    return decode


def inspecting_executor(
    runtime: DbosRuntime, output: bytes = b"done", delay_seconds: float = 0
) -> RecordingAgentExecutorV2:
    return RecordingAgentExecutorV2(
        command=emitting(output, delay_seconds=delay_seconds),
        decoder=_observing_durable_process_phase(runtime),
    )


_PROVIDER_APPENDS_ONE_BYTE = (
    "from pathlib import Path; Path(__import__('sys').argv[1]).open('ab').write(b'x')"
)


def counting_executor(counter: Path) -> RecordingAgentExecutorV2:
    """An executor whose process leaves one byte per real invocation behind."""

    return RecordingAgentExecutorV2(
        command=launching(
            sys.executable, "-c", _PROVIDER_APPENDS_ONE_BYTE, str(counter)
        )
    )


def test_attempt_is_prepared_before_controlled_executor_invocation(
    tmp_path: Path,
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        request = attempt_request(runtime)
        executor = inspecting_executor(runtime)

        outcome = execute_agent_attempt(
            agent_attempt_execution(request),
            executor,
            DbosAgentAttemptStore(runtime.engine),
            runtime.agent_process_supervisor,
            runtime_workspace_owner(runtime),
        )

        assert isinstance(outcome, AgentAttemptSucceeded)
        assert outcome.completion == RunContinues("done")
        assert len(executor.results) == 1
    finally:
        runtime.close()


def test_thirty_two_claims_invoke_one_controlled_executor(tmp_path: Path) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        request = attempt_request(runtime, "attempt/concurrent")
        executor = inspecting_executor(runtime, b"once", 0.25)
        store = DbosAgentAttemptStore(runtime.engine)
        execution = agent_attempt_execution(request)
        with ThreadPoolExecutor(max_workers=32) as pool:
            futures = [
                pool.submit(
                    execute_agent_attempt,
                    execution,
                    executor,
                    store,
                    runtime.agent_process_supervisor,
                    runtime_workspace_owner(runtime),
                )
                for _ in range(32)
            ]
            outcomes = tuple(future.result(timeout=5) for future in futures)

        assert len(executor.results) == 1
        assert sum(isinstance(value, AgentAttemptSucceeded) for value in outcomes) >= 1
        assert all(
            isinstance(value, (AgentAttemptSucceeded, AgentAttemptPossiblyRan))
            for value in outcomes
        )
    finally:
        runtime.close()


def test_reentering_after_terminal_attempt_never_authorizes_invocation(
    tmp_path: Path,
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        request = attempt_request(runtime, "attempt/replayed-claim")
        store = DbosAgentAttemptStore(runtime.engine)
        executor = inspecting_executor(runtime)

        first = execute_agent_attempt(
            agent_attempt_execution(request),
            executor,
            store,
            runtime.agent_process_supervisor,
            runtime_workspace_owner(runtime),
        )
        recovered = execute_agent_attempt(
            agent_attempt_execution(request),
            executor,
            DbosAgentAttemptStore(runtime.engine),
            runtime.agent_process_supervisor,
            runtime_workspace_owner(runtime),
        )

        assert isinstance(first, AgentAttemptSucceeded)
        assert first.completion == RunContinues("done")
        assert recovered == first
        assert len(executor.results) == 1
        assert len(executor.released_commands) == 2
    finally:
        runtime.close()


def _stage_agent_sink_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stage the one graph shape that drives a terminal agent completion.

    V1 and V2 deliberately cannot publish an Agent sink -- their agent nodes
    always carry a successor -- and V3 is not executable yet, so no document
    reaches this branch today. Keep that product boundary closed while
    exercising the real store transaction at the exact graph seam H1a opens.
    """

    terminal_graph = WorkflowGraphV2.model_construct(
        format_version=2,
        start="build",
        nodes=(
            AgentNodeV2.model_construct(
                id="build", type="agent", role="builder", job="build", next=None
            ),
        ),
    )
    for module in (agent_attempt_store_module, run_store_module):
        monkeypatch.setattr(
            module, "load_graph", lambda _session, _revision_hash: terminal_graph
        )


def test_terminal_agent_success_is_one_durable_write_and_exact_reentry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        request = attempt_request(runtime, "attempt/terminal-agent")
        execution = agent_attempt_execution(request)
        _stage_agent_sink_graph(monkeypatch)
        executor = inspecting_executor(runtime)

        first = execute_agent_attempt(
            execution,
            executor,
            DbosAgentAttemptStore(runtime.engine),
            runtime.agent_process_supervisor,
            runtime_workspace_owner(runtime),
        )
        recovered = execute_agent_attempt(
            execution,
            executor,
            DbosAgentAttemptStore(runtime.engine),
            runtime.agent_process_supervisor,
            runtime_workspace_owner(runtime),
        )

        with runtime.engine.connect() as connection:
            attempt = connection.execute(sa.select(agent_attempts)).mappings().one()
            event = connection.execute(sa.select(run_events)).mappings().one()
            run = connection.execute(sa.select(runs)).mappings().one()
            receipt_count = connection.scalar(
                sa.select(sa.func.count()).select_from(agent_receipts_v2)
            )

        assert isinstance(first, AgentAttemptSucceeded)
        assert first.completion == RunCompletes()
        assert first.attempt.receipt_hash is not None
        assert recovered == first
        assert len(executor.results) == 1
        assert (
            attempt["state"],
            attempt["state_version"],
            attempt["process_phase"],
            attempt["receipt_hash"],
            event["event_sequence"],
            event["event_kind"],
            event["node_id"],
            event["agent_attempt_id"],
            event["attempt_ordinal"],
            event["payload"],
            run["state"],
            run["current_node_id"],
            run["state_version"],
            run["last_event_sequence"],
            receipt_count,
        ) == (
            "SUCCEEDED",
            4,
            "PROCESS_OBSERVED",
            first.attempt.receipt_hash.value,
            1,
            "AGENT_COMPLETED",
            "build",
            execution.attempt_id.value,
            1,
            b"done",
            "COMPLETED",
            "build",
            1,
            1,
            1,
        )
    finally:
        runtime.close()


def test_a_claim_replayed_from_a_lost_incarnation_never_authorizes_invocation(
    tmp_path: Path,
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        request = attempt_request(runtime, "attempt/lost-incarnation")
        execution = agent_attempt_execution(request)
        lost_incarnation = DbosAgentAttemptStore(runtime.engine)
        lost_incarnation.prepare(execution)
        assert isinstance(
            lost_incarnation.claim(execution), AgentAttemptClaimedByThisCall
        )

        counter = tmp_path / "invocations"
        executor = counting_executor(counter)
        outcome = execute_agent_attempt(
            execution,
            executor,
            DbosAgentAttemptStore(runtime.engine),
            runtime.agent_process_supervisor,
            runtime_workspace_owner(runtime),
        )

        assert isinstance(outcome, AgentAttemptPossiblyRan)
        assert not counter.exists()
        assert len(executor.released_commands) == 1
    finally:
        runtime.close()


def test_the_v2_node_reaches_its_launch_boundary_by_one_application_call() -> None:
    """Gate the wiring no running test can show, and claim nothing more.

    That a replayed claim launches nothing is behavior, and it is proven twice
    above and once against a real crashed incarnation in tests/crash. None of
    those proofs notice if the durable workflow stops asking
    ``execute_agent_attempt`` and re-decides claim and launch inside its own
    transaction step, or asks it a second time: every one of them calls the
    application boundary itself, and a second launch inside one node execution
    is exactly the duplicate this branch must not contain. This is a narrow
    source gate on that one wiring decision -- it proves the call site is
    written exactly once, not the invariant behind it -- and it stands until
    the node binding moves into the core (#86), which is exactly the change
    that could lose the boundary while the suite stays green.
    """

    workflow_module = (
        Path(__file__).parents[2] / "src/atelier2/adapters/dbos/workflow.py"
    )
    tree = ast.parse(workflow_module.read_text(encoding="utf-8"), workflow_module.name)
    durable_node = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "durable_node"
    )
    v2_branch = next(
        node
        for node in ast.walk(durable_node)
        if isinstance(node, ast.If) and _branch_binding_type(node) == "agent-v2"
    )
    calls = _calls_by_name(v2_branch)

    assert calls["execute_agent_attempt"] == 1
    assert not calls.keys() & {"run_tx_step", "claim", "launch_and_wait"}
    assert not {name for name in calls if name.startswith("commit_")}


def _branch_binding_type(branch: ast.If) -> str | None:
    test = branch.test
    if not isinstance(test, ast.Compare) or len(test.comparators) != 1:
        return None
    compared = test.comparators[0]
    if not isinstance(compared, ast.Constant) or not isinstance(compared.value, str):
        return None
    return compared.value


def _calls_by_name(branch: ast.If) -> Counter[str]:
    names: Counter[str] = Counter()
    for node in ast.walk(branch):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            names[node.func.id] += 1
        elif isinstance(node.func, ast.Attribute):
            names[node.func.attr] += 1
    return names


def test_current_attempt_projection_maps_armed_and_rejects_broken_id(
    tmp_path: Path,
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        request = attempt_request(runtime, "attempt/projection")
        store = DbosAgentAttemptStore(runtime.engine)
        store.prepare(agent_attempt_execution(request))
        store.claim(agent_attempt_execution(request))

        found = durable_queries(runtime.engine).get_run(request.run_id)

        assert isinstance(found, RunFound)
        attempt = found.projection.current_agent_attempt
        assert attempt is not None
        assert attempt.state == "POSSIBLY_RAN"
        assert attempt.failure_code is None
        assert attempt.request_hash == request.request_hash

        with runtime.engine.begin() as connection:
            connection.exec_driver_sql("DROP TRIGGER agent_attempts_state_transition")
            connection.execute(agent_attempts.update().values(attempt_id="f" * 64))
        assert isinstance(
            durable_queries(runtime.engine).get_run(request.run_id),
            QueryDurableStateCorrupt,
        )
    finally:
        runtime.close()


@pytest.mark.parametrize("known_failure", (False, True))
def test_terminal_attempt_commit_is_atomic_and_matches_success_or_known_failure(
    tmp_path: Path, known_failure: bool
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        request = attempt_request(runtime, f"attempt/terminal/{known_failure}")
        store = DbosAgentAttemptStore(runtime.engine)

        terminal = RecordingAgentExecutorV2(
            command=launching(sys.executable, "-c", "raise SystemExit(7)"),
            decoder=answering(
                AgentExecutionFailure(
                    AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY
                )
                if known_failure
                else AgentExecutionResult(b"done")
            ),
        )

        outcome = execute_agent_attempt(
            agent_attempt_execution(request),
            terminal,
            store,
            runtime.agent_process_supervisor,
            runtime_workspace_owner(runtime),
        )
        assert len(terminal.released_commands) == 1
        with runtime.engine.connect() as connection:
            attempt = connection.execute(sa.select(agent_attempts)).mappings().one()
            event = connection.execute(sa.select(run_events)).mappings().one()
            run = connection.execute(sa.select(runs)).mappings().one()
            receipt_count = connection.scalar(
                sa.select(sa.func.count()).select_from(agent_receipts_v2)
            )

        if known_failure:
            assert isinstance(outcome, AgentAttemptFailed)
            assert (attempt["state"], event["event_kind"], receipt_count) == (
                "FAILED",
                "AGENT_FAILED",
                0,
            )
            assert (run["current_node_id"], run["state_version"]) == ("build", 1)
        else:
            assert isinstance(outcome, AgentAttemptSucceeded)
            assert (attempt["state"], event["event_kind"], receipt_count) == (
                "SUCCEEDED",
                "AGENT_COMPLETED",
                1,
            )
            assert (run["current_node_id"], run["state_version"]) == ("done", 1)
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("terminal", "failpoint", "trigger"),
    (
        (
            "success",
            "receipt",
            (
                "CREATE TRIGGER fail_receipt BEFORE INSERT ON agent_receipts_v2 "
                "BEGIN SELECT RAISE(ABORT, 'failpoint'); END"
            ),
        ),
        (
            "success",
            "attempt",
            (
                "CREATE TRIGGER fail_attempt BEFORE UPDATE ON agent_attempts "
                "WHEN NEW.state='SUCCEEDED' "
                "BEGIN SELECT RAISE(ABORT, 'failpoint'); END"
            ),
        ),
        (
            "success",
            "run",
            (
                "CREATE TRIGGER fail_run BEFORE UPDATE ON runs "
                "BEGIN SELECT RAISE(ABORT, 'failpoint'); END"
            ),
        ),
        (
            "success",
            "event",
            (
                "CREATE TRIGGER fail_event BEFORE INSERT ON run_events "
                "BEGIN SELECT RAISE(ABORT, 'failpoint'); END"
            ),
        ),
        (
            "failure",
            "attempt",
            (
                "CREATE TRIGGER fail_attempt BEFORE UPDATE ON agent_attempts "
                "WHEN NEW.state='FAILED' "
                "BEGIN SELECT RAISE(ABORT, 'failpoint'); END"
            ),
        ),
        (
            "failure",
            "run",
            (
                "CREATE TRIGGER fail_run BEFORE UPDATE ON runs "
                "BEGIN SELECT RAISE(ABORT, 'failpoint'); END"
            ),
        ),
        (
            "failure",
            "event",
            (
                "CREATE TRIGGER fail_event BEFORE INSERT ON run_events "
                "BEGIN SELECT RAISE(ABORT, 'failpoint'); END"
            ),
        ),
    ),
)
def test_each_terminal_write_failpoint_rolls_back_the_whole_attempt(
    tmp_path: Path, terminal: str, failpoint: str, trigger: str
) -> None:
    runtime = attempt_runtime(tmp_path / terminal / failpoint)
    runtime.initialize_storage()
    try:
        request = attempt_request(runtime, f"attempt/failpoint/{failpoint}")
        store = DbosAgentAttemptStore(runtime.engine)
        store.prepare(agent_attempt_execution(request))
        store.claim(agent_attempt_execution(request))
        with runtime.engine.begin() as connection:
            connection.exec_driver_sql(trigger)

        with pytest.raises(DatabaseError, match="failpoint"):
            if terminal == "success":
                store.complete_success(
                    agent_attempt_execution(request), AgentExecutionResult(b"done")
                )
            else:
                store.complete_known_failure(agent_attempt_execution(request))

        with runtime.engine.connect() as connection:
            attempt = connection.execute(sa.select(agent_attempts)).mappings().one()
            run = connection.execute(sa.select(runs)).mappings().one()
            receipt_count = connection.scalar(
                sa.select(sa.func.count()).select_from(agent_receipts_v2)
            )
            event_count = connection.scalar(
                sa.select(sa.func.count()).select_from(run_events)
            )
        assert (attempt["state"], attempt["state_version"]) == ("LAUNCH_ARMED", 1)
        assert (run["current_node_id"], run["state_version"]) == ("build", 0)
        assert (receipt_count, event_count) == (0, 0)
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "mutation",
    (
        "UPDATE agent_attempts SET executor_operational_identity='other'",
        (
            "UPDATE agent_attempts SET state='FAILED', state_version=2, "
            "failure_code='PROCESS_EXITED_UNSUCCESSFULLY'"
        ),
        "DELETE FROM agent_attempts",
    ),
)
def test_attempt_trigger_rejects_binding_skips_and_deletion(
    tmp_path: Path, mutation: str
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        request = attempt_request(runtime, "attempt/trigger-canary")
        store = DbosAgentAttemptStore(runtime.engine)
        store.prepare(agent_attempt_execution(request))

        with pytest.raises(IntegrityError), runtime.engine.begin() as connection:
            connection.exec_driver_sql(mutation)

        with runtime.engine.connect() as connection:
            record = connection.execute(sa.select(agent_attempts)).mappings().one()
        assert (
            record["state"],
            record["state_version"],
            record["executor_operational_identity"],
        ) == ("PREPARED", 0, request.executor_operational_identity.value)
    finally:
        runtime.close()


def test_attempt_trigger_rejects_terminal_rewrite_and_mismatched_receipt(
    tmp_path: Path,
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        request = attempt_request(runtime, "attempt/trigger-terminal")
        store = DbosAgentAttemptStore(runtime.engine)
        store.prepare(agent_attempt_execution(request))
        store.claim(agent_attempt_execution(request))
        failed = store.complete_known_failure(agent_attempt_execution(request))

        with pytest.raises(IntegrityError), runtime.engine.begin() as connection:
            connection.execute(
                agent_attempts.update().values(
                    state="LAUNCH_ARMED",
                    state_version=1,
                    failure_code=None,
                )
            )
        assert store.claim(agent_attempt_execution(request)) == failed

        completed_request = attempt_request(runtime, "attempt/trigger-receipt-source")
        store.prepare(agent_attempt_execution(completed_request))
        store.claim(agent_attempt_execution(completed_request))
        completed = store.complete_success(
            agent_attempt_execution(completed_request), AgentExecutionResult(b"wrong")
        )
        assert completed.attempt.receipt_hash is not None

        second = attempt_request(runtime, "attempt/trigger-receipt-target")
        store.prepare(agent_attempt_execution(second))
        store.claim(agent_attempt_execution(second))
        with (
            runtime.engine.begin() as connection,
            pytest.raises(IntegrityError),
        ):
            connection.execute(
                agent_attempts.update()
                .where(agent_attempts.c.request_hash == second.request_hash.value)
                .values(
                    state="SUCCEEDED",
                    state_version=2,
                    receipt_hash=completed.attempt.receipt_hash.value,
                )
            )
    finally:
        runtime.close()


def test_reentry_after_a_terminal_success_refuses_a_run_head_that_disagrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A terminal agent success is one durable write: the attempt CAS, the
    # AGENT_COMPLETED event and the completed run head land together. If a run
    # head ever disagreed with a SUCCEEDED attempt, the torn half must surface
    # rather than be reconstructed into a success nobody durably made.
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        request = attempt_request(runtime, "attempt/terminal-reentry")
        execution = agent_attempt_execution(request)
        _stage_agent_sink_graph(monkeypatch)
        executor = inspecting_executor(runtime)
        store = DbosAgentAttemptStore(runtime.engine)

        first = execute_agent_attempt(
            execution,
            executor,
            store,
            runtime.agent_process_supervisor,
            runtime_workspace_owner(runtime),
        )
        assert isinstance(first, AgentAttemptSucceeded)
        assert first.completion == RunCompletes()

        with runtime.engine.begin() as connection:
            connection.execute(
                runs.update().values(state=RunState.STARTED.value, terminal_hash=None)
            )

        with pytest.raises(RunTransitionConflict):
            execute_agent_attempt(
                execution,
                executor,
                DbosAgentAttemptStore(runtime.engine),
                runtime.agent_process_supervisor,
                runtime_workspace_owner(runtime),
            )

        assert len(executor.results) == 1
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("status", "driverless"),
    (
        ("PENDING", False),
        ("ENQUEUED", False),
        ("DELAYED", False),
        ("SUCCESS", True),
        ("ERROR", True),
        ("CANCELLED", True),
        ("MAX_RECOVERY_ATTEMPTS_EXCEEDED", True),
    ),
)
def test_an_attempt_is_driverless_once_its_workflow_can_no_longer_move_it(
    tmp_path: Path, status: str, driverless: bool
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        request = attempt_request(runtime)
        store = DbosAgentAttemptStore(runtime.engine)
        attempt = store.prepare(agent_attempt_execution(request))
        _record_driving_workflow(runtime, driving_workflow_id(attempt), status)

        assert store.driverless_attempts() == ((attempt,) if driverless else ())
    finally:
        runtime.close()


def test_an_attempt_whose_workflow_never_reached_the_store_is_driverless(
    tmp_path: Path,
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        request = attempt_request(runtime)
        store = DbosAgentAttemptStore(runtime.engine)
        attempt = store.prepare(agent_attempt_execution(request))

        assert store.driverless_attempts() == (attempt,)
    finally:
        runtime.close()


def test_a_terminal_attempt_is_never_driverless(tmp_path: Path) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        request = attempt_request(runtime)
        store = DbosAgentAttemptStore(runtime.engine)
        execute_agent_attempt(
            agent_attempt_execution(request),
            inspecting_executor(runtime),
            store,
            runtime.agent_process_supervisor,
            runtime_workspace_owner(runtime),
        )

        assert store.driverless_attempts() == ()
    finally:
        runtime.close()
