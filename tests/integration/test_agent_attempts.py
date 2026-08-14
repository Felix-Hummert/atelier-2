from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DatabaseError, IntegrityError

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.queries import DbosQueries
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
)
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
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
from atelier2.contracts.runs import RunId, WorkflowRevision
from atelier2.ports.agent_attempts import (
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
    AgentProcessExited,
    AgentProcessInvocation,
)
from atelier2.ports.durable_runs import DurableRunCreated, StartPublishedRunRequestV2
from atelier2.ports.run_queries import RunFound
from atelier2.ports.workflow_revisions import QueryDurableStateCorrupt
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_attempt_execution,
)

_DOCUMENT = b"""format_version: 2
start: build
nodes:
  - {id: done, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: build, type: agent, role: builder, job: build, next: done}
"""


def attempt_runtime(root: Path) -> DbosRuntime:
    return DbosRuntime(
        DbosRuntimeSettings(root / "atelier.sqlite", "attempt-test"),
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
        "opus", auth.revision_hash, AgentExecutorRevision("claude-cli/v1")
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


@dataclass
class _InspectingExecutor:
    runtime: DbosRuntime
    output: bytes = b"done"
    delay_seconds: float = 0
    calls: int = 0

    def prepare_process(
        self, request: AgentExecutionRequestV2
    ) -> AgentProcessInvocation:
        del request
        return AgentProcessInvocation(
            (
                sys.executable,
                "-c",
                "import os,time,sys; time.sleep(float(sys.argv[1])); os.write(1,bytes.fromhex(sys.argv[2]))",
                str(self.delay_seconds),
                self.output.hex(),
            ),
            Path.cwd(),
        )

    def decode_process_completion(
        self, completion: AgentProcessExited
    ) -> AgentExecutionResult:
        with self.runtime.engine.connect() as connection:
            state = connection.scalar(sa.select(agent_attempts.c.process_phase))
        assert state == "PROCESS_OBSERVED"
        self.calls += 1
        return AgentExecutionResult(completion.standard_output)

    def close(self) -> None:
        pass


def test_attempt_is_prepared_before_controlled_executor_invocation(
    tmp_path: Path,
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        request = attempt_request(runtime)
        executor = _InspectingExecutor(runtime)

        outcome = execute_agent_attempt(
            agent_attempt_execution(request),
            executor,
            DbosAgentAttemptStore(runtime.engine),
            runtime.agent_process_supervisor,
        )

        assert isinstance(outcome, AgentAttemptSucceeded)
        assert executor.calls == 1
    finally:
        runtime.close()


def test_thirty_two_claims_invoke_one_controlled_executor(tmp_path: Path) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        request = attempt_request(runtime, "attempt/concurrent")
        executor = _InspectingExecutor(runtime, b"once", 0.25)
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
                )
                for _ in range(32)
            ]
            outcomes = tuple(future.result(timeout=5) for future in futures)

        assert executor.calls == 1
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
        executor = _InspectingExecutor(runtime)

        first = execute_agent_attempt(
            agent_attempt_execution(request),
            executor,
            store,
            runtime.agent_process_supervisor,
        )
        recovered = execute_agent_attempt(
            agent_attempt_execution(request),
            executor,
            store,
            runtime.agent_process_supervisor,
        )

        assert isinstance(first, AgentAttemptSucceeded)
        assert recovered == first
        assert executor.calls == 1
    finally:
        runtime.close()


def test_dbos_replayed_claim_result_can_never_authorize_invocation() -> None:
    source = Path(__file__).parents[2] / "src/atelier2/adapters/dbos/workflow.py"
    workflow_source = source.read_text(encoding="utf-8")
    durable_node_source = workflow_source[
        workflow_source.index("    def durable_node(") :
    ]

    assert "execute_agent_attempt(" in durable_node_source
    assert (
        "run_tx_step"
        not in durable_node_source[
            durable_node_source.index(
                "outcome = execute_agent_attempt("
            ) : durable_node_source.index('if binding["type"] == "action"')
        ]
    )
    assert "commit_agent_completed_v2" not in workflow_source


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

        found = DbosQueries(runtime.engine).get_run(request.run_id)

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
            DbosQueries(runtime.engine).get_run(request.run_id),
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

        class _TerminalExecutor:
            def prepare_process(
                self, request: AgentExecutionRequestV2
            ) -> AgentProcessInvocation:
                del request
                return AgentProcessInvocation(
                    (sys.executable, "-c", "raise SystemExit(7)"), Path.cwd()
                )

            def decode_process_completion(
                self, completion: AgentProcessExited
            ) -> AgentExecutionResult | AgentExecutionFailure:
                if known_failure:
                    return AgentExecutionFailure(
                        AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY
                    )
                return AgentExecutionResult(b"done")

            def close(self) -> None:
                pass

        outcome = execute_agent_attempt(
            agent_attempt_execution(request),
            _TerminalExecutor(),
            store,
            runtime.agent_process_supervisor,
        )
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
                store.complete_known_failure(
                    agent_attempt_execution(request),
                    AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY,
                )

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
        failed = store.complete_known_failure(
            agent_attempt_execution(request),
            AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY,
        )

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
