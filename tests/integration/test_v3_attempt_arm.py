"""A V3 agent node reaches the durable attempt path.

H1c's first bound question: today the attempt store refuses a V3 run at its own
front door -- `_validate_request` accepts only a `RunV2` with a
`WorkflowGraphV2`, so a V3 agent node cannot even be prepared, let alone run.

The receipt chain Codex designed on top of this door --

    AgentReceiptV2  (the provider's truth, unchanged)
      -> NodeReceipt.agent_receipt_hash  (node-receipt/v3, Cut B's form)
        -> RunEvent.node_receipt_hash
          -> terminal_hash               (unchanged, #110's chain)

-- is not written here, and the reason is a measured absence rather than a
choice: a `NodeReceipt` names a `node-execution-request/v3` hash and a
`context-package/v3` hash, and no production code authors either record. ADR 0006
binds the manifest to material written once before START, and the request binds
the run configuration revision `RunV3` already documents as unreconstructed. The
attempt path holds an `AgentExecutionRequestV2`, whose hash is framed under a
different domain, so linking it in would publish a receipt whose own request hash
recomputes to nothing. The chain waits on that author, not on this door.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.run_store import (
    RunTransitionConflict,
    run_from_record_with_bindings,
)
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import runs
from atelier2.adapters.dbos.starter import DbosDurableRunStarter
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.contracts.agent_attempts import AgentAttemptId, AgentAttemptState
from atelier2.contracts.agents import (
    AgentExecutionRequestV2,
    AgentExecutionResult,
    AgentExecutorOperationalIdentity,
    ResolvedAgentBinding,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.executions import AgentAttemptExecution, NodeExecutionId
from atelier2.contracts.run_bindings import RunV3
from atelier2.contracts.runs import (
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.contracts.workflows import RunCompletes
from atelier2.ports.durable_runs import StartPublishedRunRequestV2
from tests.integration.test_v3_agent_start import publish
from tests.scenarios.agents import (
    agent_scratch_root,
    failing_agent_executor_factory,
)

RUN = RunId("v3/attempt")
INSTRUCTION = b"Do the one thing this chain is for."


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    started = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "h1c-test",
            agent_scratch_root=agent_scratch_root(tmp_path),
        ),
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        ExactOutputAgentExecutorFactory(),
        (failing_agent_executor_factory("exact", []),),
    )
    started.initialize_storage()
    try:
        yield started
    finally:
        started.close()


def started_v3_attempt(
    runtime: DbosRuntime,
) -> tuple[WorkflowRevision, AgentAttemptExecution]:
    """One started V3 run, and the attempt its agent node would run under."""
    workflow, bindings = publish(runtime)
    DbosDurableRunStarter(
        runtime.engine, runtime.settings, runtime.agent_executor_registry
    ).start_v3_foundation(
        StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings)
    )
    revision_hash = WorkflowRevisionHash(workflow.revision_hash.value)
    with runtime.engine.connect() as connection:
        record = (
            connection.execute(sa.select(runs).where(runs.c.run_id == RUN.value))
            .mappings()
            .one()
        )
        run = run_from_record_with_bindings(connection, record)
    assert isinstance(run, RunV3)
    binding = run.agent_bindings[0]
    request = AgentExecutionRequestV2(
        NodeExecutionId.for_node(RUN, revision_hash, "implement"),
        RUN,
        revision_hash,
        "implement",
        ResolvedAgentBinding(binding.role, binding.configuration, binding.auth_profile),
        AgentExecutorOperationalIdentity("exact-operation"),
        INSTRUCTION,
    )
    execution = AgentAttemptExecution(
        request,
        AgentAttemptId.for_execution(request.node_execution_id, request.request_hash),
        1,
    )
    return workflow, execution


@pytest.mark.proves("a-v3-agent-node-reaches-the-durable-attempt-path")
def test_a_v3_attempt_is_admitted_by_the_attempt_store(runtime: DbosRuntime) -> None:
    """The door that refuses today: 'agent attempt requires a V2 run'."""
    _workflow, execution = started_v3_attempt(runtime)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)

    prepared = store.prepare(execution)

    assert prepared.node_id == "implement"
    assert prepared.attempt_ordinal == 1


@pytest.mark.proves("a-v3-agent-node-reaches-the-durable-attempt-path")
def test_an_attempt_for_a_run_that_does_not_exist_is_still_refused(
    runtime: DbosRuntime,
) -> None:
    """Widening the door must not open it: an absent run is still no run."""
    _workflow, execution = started_v3_attempt(runtime)
    absent = RunId("v3/absent")
    request = AgentExecutionRequestV2(
        NodeExecutionId.for_node(
            absent, execution.request.workflow_revision_hash, "implement"
        ),
        absent,
        execution.request.workflow_revision_hash,
        "implement",
        execution.request.resolved_binding,
        execution.request.executor_operational_identity,
        INSTRUCTION,
    )
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)

    with pytest.raises(RunTransitionConflict):
        store.prepare(
            AgentAttemptExecution(
                request,
                AgentAttemptId.for_execution(
                    request.node_execution_id, request.request_hash
                ),
                1,
            )
        )


@pytest.mark.proves("a-completed-v3-attempt-reaches-the-runs-terminal-hash")
def test_a_completed_v3_attempt_carries_its_run_to_the_terminal_hash(
    runtime: DbosRuntime,
) -> None:
    """The provider's receipt and the run's terminal hash, from one V3 attempt."""
    _workflow, execution = started_v3_attempt(runtime)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)
    store.prepare(execution)
    store.claim(execution)

    succeeded = store.complete_success(
        execution, AgentExecutionResult(b"the exact provider bytes")
    )

    assert succeeded.attempt.state is AgentAttemptState.SUCCEEDED
    assert succeeded.completion == RunCompletes()
    with runtime.engine.connect() as connection:
        run = (
            connection.execute(sa.select(runs).where(runs.c.run_id == RUN.value))
            .mappings()
            .one()
        )
    assert run["state"] == RunState.COMPLETED.value
    assert run["terminal_hash"] is not None
