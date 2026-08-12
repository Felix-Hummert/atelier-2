from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import replace

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from atelier2.adapters.dbos.run_store import (
    AgentReceiptConflict,
    commit_agent_completed,
)
from atelier2.adapters.dbos.runtime import (
    DbosRuntime,
    DbosRuntimeBindingConflict,
    DbosRuntimeSettings,
)
from atelier2.adapters.dbos.schema import (
    agent_receipts,
    run_events,
    runs,
)
from atelier2.adapters.dbos.starter import DbosDurableRunStarter
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.adapters.exact_output_agent import (
    EXACT_OUTPUT_EXECUTOR_BINDING,
    ExactOutputAgentExecutorFactory,
)
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.contracts.agents import (
    AgentExecutionRequest,
    AgentExecutionResult,
    AgentExecutorBinding,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.runs import RunId, StartRunRequest, WorkflowRevision
from tests.scenarios.agents import configured_agent_request

DOCUMENT = b"""format_version: 1
start: agent
nodes:
  - {id: wait, type: wait, answer_type: integer, next: final}
  - {id: final, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: agent, type: agent, job: job-17, output: draft-17, next: wait}
"""


class CapturingAgentExecutorFactory:
    binding = EXACT_OUTPUT_EXECUTOR_BINDING

    def __init__(self) -> None:
        self.requests: list[AgentExecutionRequest] = []

    def open(self) -> CapturingAgentExecutorFactory:
        return self

    def execute(self, request: AgentExecutionRequest) -> AgentExecutionResult:
        self.requests.append(request)
        return AgentExecutionResult(request.exact_output.output_bytes)

    def close(self) -> None:
        return


class RefusedChangedAgentExecutorFactory:
    binding = AgentExecutorBinding(
        AgentExecutorRevision("exact-output/v2"),
        AgentExecutorOperationalIdentity("format-version-1"),
    )

    def open(self) -> CapturingAgentExecutorFactory:
        raise AssertionError("durable binding conflict opened the changed executor")


@pytest.fixture
def runtime(tmp_path) -> Iterator[DbosRuntime]:
    configured = DbosRuntime(
        DbosRuntimeSettings(tmp_path / "atelier.sqlite", "executor-A"),
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        ExactOutputAgentExecutorFactory(),
    )
    try:
        yield configured
    finally:
        configured.close()


def seeded(runtime: DbosRuntime) -> tuple[RunId, WorkflowRevision]:
    run_id = RunId("run-1")
    revision = WorkflowRevision(DOCUMENT)
    runtime.initialize_storage()
    runtime_starter = DbosDurableRunStarter(runtime.engine, runtime.settings)
    runtime_starter.start(StartRunRequest(run_id, revision))
    return run_id, revision


def test_unchanged_v1_workflow_runs_through_the_injected_executor(
    tmp_path,
) -> None:
    factory = CapturingAgentExecutorFactory()
    configured = DbosRuntime(
        DbosRuntimeSettings(tmp_path / "injected.sqlite", "executor-A"),
        LoopbackEffectAdapterFactory(
            tmp_path / "injected-effects.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        factory,
    )
    try:
        run_id, revision = seeded(configured)
        configured.launch()
        deadline = time.monotonic() + 8
        observed = ""
        while observed != "WAITING_INPUT" and time.monotonic() < deadline:
            with configured.engine.connect() as connection:
                observed = str(connection.scalar(sa.select(runs.c.state)))
            time.sleep(0.025)
        assert observed == "WAITING_INPUT"
        assert len(factory.requests) == 1
        request = factory.requests[0]
        assert request.run_id == run_id
        assert request.workflow_revision_hash == revision.revision_hash
        assert request.node_id == "agent"
        assert request.job_bytes == b"job-17"
        assert request.exact_output.output_bytes == b"draft-17"
        with configured.engine.connect() as connection:
            assert connection.execute(
                sa.select(
                    run_events.c.event_kind,
                    run_events.c.payload,
                    runs.c.current_node_id,
                )
                .select_from(
                    run_events.join(runs, run_events.c.run_id == runs.c.run_id)
                )
                .where(run_events.c.event_kind == "AGENT_COMPLETED")
            ).one() == ("AGENT_COMPLETED", b"draft-17", "wait")
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(agent_receipts)
                )
                == 1
            )
    finally:
        configured.close()

    with pytest.raises(DbosRuntimeBindingConflict, match="durable agent receipts"):
        DbosRuntime(
            DbosRuntimeSettings(tmp_path / "injected.sqlite", "executor-A"),
            LoopbackEffectAdapterFactory(
                tmp_path / "injected-effects.sqlite",
                AdapterRevision("loopback-v1"),
                EffectDestination("loopback-test"),
            ),
            RefusedChangedAgentExecutorFactory(),
        )


@pytest.mark.parametrize(
    ("trigger_name", "trigger_sql"),
    [
        (
            "fail_agent_receipt",
            (
                "CREATE TRIGGER fail_agent_receipt BEFORE INSERT ON agent_receipts "
                "BEGIN SELECT RAISE(ABORT, 'injected receipt failure'); END"
            ),
        ),
        (
            "fail_agent_run",
            (
                "CREATE TRIGGER fail_agent_run BEFORE UPDATE ON runs "
                "BEGIN SELECT RAISE(ABORT, 'injected run failure'); END"
            ),
        ),
        (
            "fail_agent_event",
            (
                "CREATE TRIGGER fail_agent_event BEFORE INSERT ON run_events "
                "WHEN NEW.event_kind='AGENT_COMPLETED' "
                "BEGIN SELECT RAISE(ABORT, 'injected event failure'); END"
            ),
        ),
    ],
)
def test_receipt_event_and_successor_are_atomic_at_each_write_boundary(
    runtime, trigger_name: str, trigger_sql: str
) -> None:
    run_id, revision = seeded(runtime)
    with runtime.engine.begin() as connection:
        request = configured_agent_request(
            connection, run_id, revision.revision_hash, "agent"
        )
        connection.execute(sa.text(trigger_sql))

    with (
        pytest.raises(IntegrityError),
        canonical_write_transaction(runtime.engine) as connection,
    ):
        commit_agent_completed(
            connection,
            request,
            EXACT_OUTPUT_EXECUTOR_BINDING,
            AgentExecutionResult(b"draft-17"),
        )

    with runtime.engine.begin() as connection:
        connection.execute(sa.text(f"DROP TRIGGER {trigger_name}"))
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(agent_receipts))
            == 0
        )
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(run_events)) == 0
        )
        assert connection.execute(
            sa.select(
                runs.c.current_node_id,
                runs.c.state_version,
                runs.c.last_event_sequence,
            )
        ).one() == ("agent", 0, 0)


def test_exact_retry_is_one_receipt_and_contradictions_mutate_nothing(runtime) -> None:
    run_id, revision = seeded(runtime)
    with canonical_write_transaction(runtime.engine) as connection:
        request = configured_agent_request(
            connection, run_id, revision.revision_hash, "agent"
        )
        first = commit_agent_completed(
            connection,
            request,
            EXACT_OUTPUT_EXECUTOR_BINDING,
            AgentExecutionResult(b"draft-17"),
        )
    with canonical_write_transaction(runtime.engine) as connection:
        retried = commit_agent_completed(
            connection,
            request,
            EXACT_OUTPUT_EXECUTOR_BINDING,
            AgentExecutionResult(b"draft-17"),
        )
    assert retried == first

    contradictions = (
        (
            replace(request, job_bytes=b"different-job"),
            EXACT_OUTPUT_EXECUTOR_BINDING,
            AgentExecutionResult(b"draft-17"),
        ),
        (
            request,
            AgentExecutorBinding(
                AgentExecutorRevision("exact-output/v2"),
                AgentExecutorOperationalIdentity("format-version-1"),
            ),
            AgentExecutionResult(b"draft-17"),
        ),
        (
            request,
            EXACT_OUTPUT_EXECUTOR_BINDING,
            AgentExecutionResult(b"different-result"),
        ),
    )
    with runtime.engine.connect() as connection:
        before = tuple(connection.execute(sa.select(agent_receipts)).all())
        before_events = tuple(connection.execute(sa.select(run_events)).all())
        before_run = connection.execute(sa.select(runs)).one()
    for conflicting_request, binding, result in contradictions:
        with (
            pytest.raises(AgentReceiptConflict),
            canonical_write_transaction(runtime.engine) as connection,
        ):
            commit_agent_completed(connection, conflicting_request, binding, result)
        with runtime.engine.connect() as connection:
            assert tuple(connection.execute(sa.select(agent_receipts)).all()) == before
            assert (
                tuple(connection.execute(sa.select(run_events)).all()) == before_events
            )
            assert connection.execute(sa.select(runs)).one() == before_run
