"""Serve-start recovery of leftover INTERRUPTED runs.

The leftover STARTED rows after #339 are this class and its neighbours:
an attempt already INTERRUPTED under atelier2-driver-lost, a silent successor
whose durable node workflow is gone — with or without a durable request on
the current node — and the two shapes the #339 guardians already protect. A
silent successor whose node workflow is still pending under the running
application version is named, not invented. This file prepares those rows
and asks serve start to end only what it can name.

#439 P3 adds one more family under the same net: a `CANCELLED` or
`INTERRUPTED` current-node attempt under an operator run-cancel command with
no replacement in flight lifts its run to `RunState.CANCELLED`, not
`RunState.FAILED` -- the command's identity decides the word, not which of
the two the cleanup disposition happened to leave the attempt in. The normal
store path lifts that run in the same transaction as its own attestation
(`agent_attempt_store.py`), so reaching this net's own leftover shape needs
the exact revert `leftover_failed` already uses below, standing in for a row
a store from before that in-transaction lift existed would leave behind.

#645 adds the Action shape this net used to misread: a healthy V3 run standing
on an Action node has no attempt of its own and never will, so it looks exactly
like a gap -- but the workflow that owes it its next move is the effect
workflow, not any node workflow. Reading only node workflows named that run
dead and failed it. `in_flight_action` below prepares that exact row.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.run_store import run_from_record_with_bindings
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import (
    _PRODUCT_TRIGGERS,
    agent_attempts,
    effect_intents,
    node_execution_requests_v3,
    node_receipts_v3,
    run_events,
    runs,
    workflow_revisions,
)
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.dbos.uncontinuable_runs import DbosUncontinuableRunStore
from atelier2.adapters.dbos.workflow_ids import (
    effect_workflow_id_for,
    node_workflow_id_for,
)
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.application.converge_uncontinuable_runs import (
    converge_uncontinuable_runs,
)
from atelier2.contracts.adapter_operations_v3 import AdapterOperationName
from atelier2.contracts.agent_attempts import (
    REPLACEMENT_AGENT_ATTEMPT_ORDINAL,
    STOP_AFTER_DRIVER_LOSS,
    AgentAttemptCancellationDisposition,
    AgentAttemptId,
    AgentAttemptProcessPhase,
    AgentAttemptRedriveState,
    AgentAttemptReplacement,
    AgentAttemptState,
    AgentProcessOwnerId,
    CancelAgentAttemptRequest,
    RunnerEvidenceAcceptancePhase,
    WatchdogGenerationId,
)
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutionRequestV2,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.contracts.effects import (
    AdapterRevision,
    EffectDestination,
    EffectIntent,
    EffectIntentState,
)
from atelier2.contracts.executions import (
    AgentAttemptExecution,
    NodeExecutionId,
    RunEventKind,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.node_records_v3 import PersistedReceiptDisposition
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.run_bindings import RunV3
from atelier2.contracts.run_cancellations import RunCancelCommandId
from atelier2.contracts.runs import (
    FIRST_ROUND_ORDINAL,
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.contracts.stored_node_receipt_reasons import (
    read_stored_node_receipt_reason,
)
from atelier2.contracts.workflow_formats import WorkflowFormatVersion
from atelier2.ports.agent_attempts import AgentAttemptCancellationAccepted
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AgentConfigurationRevisionExisting,
    AuthProfileRevisionCreated,
    AuthProfileRevisionExisting,
)
from atelier2.ports.agent_executions import AgentExecutionResult
from atelier2.ports.durable_runs import DurableRunCreated, StartPublishedRunRequestV2
from atelier2.ports.published_revisions import (
    PublishedRevisionCreated,
    PublishedRevisionExisting,
)
from tests.scenarios.agents import (
    agent_scratch_root,
    failing_agent_executor_factory,
    publish_checked_model_registry,
)
from tests.scenarios.runs import prepare_and_launch_graph_action

PLAN_SCHEMA = PublishedRevision(
    RevisionKind.SCHEMA,
    b'{"type": "object", "properties": {"steps": {"type": "integer", '
    b'"minimum": 1}}, "required": ["steps"], "additionalProperties": false}',
)
OPEN_PR_OPERATION = PublishedRevision(
    RevisionKind.ADAPTER_OPERATION,
    json.dumps({"operation": AdapterOperationName.OPEN_PR.value}).encode("utf-8"),
)
NODE = "plan"
SUCCESSOR = "review"
ACTION = "publish"
INSTRUCTION = b"Answer with the plan this node declared."
THE_ANSWER_THE_SCHEMA_ADMITS = b'{"steps": 3}'
THE_ANSWER_THE_SCHEMA_REFUSES = b"Sure! I would plan this in three steps."
OWNER = AgentProcessOwnerId("inventory-owner")
GENERATION = WatchdogGenerationId("inventory-generation")
V1_DOCUMENT = b"""format_version: 1
start: agent
nodes:
  - {id: final, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: agent, type: agent, job: implement, output: text, next: final}
"""


def planning_document() -> bytes:
    return f"""format_version: 3
name: Plan the work
nodes:
  - id: {NODE}
    type: agent
    role: builder
    mode: headless
    instruction: {INSTRUCTION.decode("ascii")}
    outputs:
      - name: plan
        schema:
          ref: plan-schema
          revision: {PLAN_SCHEMA.revision_hash.value}
""".encode()


def reviewed_planning_document() -> bytes:
    return (
        planning_document()
        + f"""  - id: {SUCCESSOR}
    type: agent
    role: builder
    mode: headless
    instruction: Judge the plan you were handed.
    depends_on: [{NODE}]
    inputs:
      - name: plan
        from: {{node: {NODE}, output: plan}}
    outputs:
      - name: verdict
        schema:
          ref: plan-schema
          revision: {PLAN_SCHEMA.revision_hash.value}
""".encode()
    )


def action_document() -> bytes:
    return (
        planning_document()
        + f"""  - id: {ACTION}
    type: action
    operation:
      ref: open-pr
      revision: {OPEN_PR_OPERATION.revision_hash.value}
    depends_on: [{NODE}]
    inputs:
      - name: body
        from: {{node: {NODE}, output: plan}}
""".encode()
    )


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    started = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "interrupted-inventory-test",
            agent_scratch_root=agent_scratch_root(tmp_path),
        ),
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        (failing_agent_executor_factory("exact", []),),
    )
    started.initialize_storage()
    try:
        yield started
    finally:
        started.close()


def _bindings(runtime: DbosRuntime) -> AgentBindingSet:
    published = DbosCatalogStore(runtime.engine).publish_revision(PLAN_SCHEMA)
    assert isinstance(
        published, (PublishedRevisionCreated, PublishedRevisionExisting)
    ), published
    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    auth = AuthProfileRevision("max", 1, ProviderId("exact"), AuthMode.SUBSCRIPTION)
    published_auth = catalog.publish_auth_profile_revision(auth)
    assert isinstance(
        published_auth,
        (AuthProfileRevisionCreated, AuthProfileRevisionExisting),
    ), published_auth
    configuration = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision("exact/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    published_configuration = catalog.publish_agent_configuration_revision(
        configuration
    )
    assert isinstance(
        published_configuration,
        (AgentConfigurationRevisionCreated, AgentConfigurationRevisionExisting),
    ), published_configuration
    publish_checked_model_registry(
        runtime.engine, ProviderId("exact"), (configuration,)
    )
    return AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )


def armed_attempt(
    runtime: DbosRuntime,
    run_id: RunId,
    document: bytes | None = None,
) -> AgentAttemptExecution:
    document = planning_document() if document is None else document
    bindings = _bindings(runtime)
    workflow = WorkflowRevision(document)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    created = DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        runtime.agent_executor_registry,
    ).start_published(
        StartPublishedRunRequestV2(run_id, workflow.revision_hash, bindings)
    )
    assert isinstance(created, DurableRunCreated), created
    revision_hash = WorkflowRevisionHash(workflow.revision_hash.value)
    with runtime.engine.connect() as connection:
        record = (
            connection.execute(sa.select(runs).where(runs.c.run_id == run_id.value))
            .mappings()
            .one()
        )
        run = run_from_record_with_bindings(connection, record)
    assert isinstance(run, RunV3)
    binding = run.agent_bindings[0]
    request = AgentExecutionRequestV2(
        NodeExecutionId.for_node(run_id, revision_hash, NODE),
        run_id,
        revision_hash,
        NODE,
        binding,
        AgentExecutorOperationalIdentity("exact-operation"),
        INSTRUCTION,
    )
    execution = AgentAttemptExecution(
        request,
        AgentAttemptId.for_execution(request.node_execution_id, request.request_hash),
        1,
    )
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)
    store.prepare(execution)
    store.bind_watchdog(execution, OWNER, GENERATION)
    store.claim(execution)
    return execution


def interrupt_current_attempt(
    runtime: DbosRuntime, execution: AgentAttemptExecution
) -> None:
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)
    armed = store.load(execution.attempt_id)
    command = CancelAgentAttemptRequest(
        execution.request.run_id,
        execution.attempt_id,
        STOP_AFTER_DRIVER_LOSS,
        armed.state_version,
        AgentAttemptReplacement.NONE,
    )
    accepted = store.request_cancellation(command)
    assert isinstance(accepted, AgentAttemptCancellationAccepted), accepted
    terminal = store.attest_cancellation_cleanup(
        command,
        AgentAttemptCancellationDisposition.OWNER_LOST_AFTER_PARENT_DEATH,
        OWNER,
        GENERATION,
    )
    assert terminal.attempt.state is AgentAttemptState.INTERRUPTED


def leftover_interrupted(runtime: DbosRuntime, run_id: RunId) -> AgentAttemptExecution:
    execution = armed_attempt(runtime, run_id)
    interrupt_current_attempt(runtime, execution)
    assert _run_state(runtime, run_id) == RunState.STARTED.value
    return execution


def leftover_failed(runtime: DbosRuntime, run_id: RunId) -> AgentAttemptExecution:
    execution = armed_attempt(runtime, run_id)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)
    store.complete_success(
        execution, AgentExecutionResult(THE_ANSWER_THE_SCHEMA_REFUSES)
    )
    with runtime.engine.connect() as connection:
        reverted = connection.execute(
            runs.update()
            .where(runs.c.run_id == run_id.value)
            .values(state=RunState.STARTED.value, terminal_hash=None)
        )
        assert reverted.rowcount == 1
        connection.commit()
    assert _run_state(runtime, run_id) == RunState.STARTED.value
    return execution


def leftover_operator_cancelled(
    runtime: DbosRuntime, run_id: RunId
) -> AgentAttemptExecution:
    """A run an operator cancel already lifted, reverted to look leftover.

    #439 P3's in-transaction lift closes this exact shape the moment
    `attest_cancellation_cleanup` attests it, so this net's own leftover
    inventory only ever sees the shape through the same revert
    `leftover_failed` uses above.
    """

    execution = armed_attempt(runtime, run_id)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)
    armed = store.load(execution.attempt_id)
    command = CancelAgentAttemptRequest(
        run_id,
        execution.attempt_id,
        RunCancelCommandId.for_key(f"operator-{run_id.value}").value,
        armed.state_version,
        AgentAttemptReplacement.NONE,
    )
    accepted = store.request_cancellation(command)
    assert isinstance(accepted, AgentAttemptCancellationAccepted), accepted
    terminal = store.attest_cancellation_cleanup(
        command,
        AgentAttemptCancellationDisposition.EXITED_BEFORE_SIGNAL,
        OWNER,
        GENERATION,
    )
    assert terminal.attempt.state is AgentAttemptState.CANCELLED
    assert _run_state(runtime, run_id) == RunState.CANCELLED.value
    with runtime.engine.connect() as connection:
        reverted = connection.execute(
            runs.update()
            .where(runs.c.run_id == run_id.value)
            .values(state=RunState.STARTED.value, terminal_hash=None)
        )
        assert reverted.rowcount == 1
        connection.commit()
    assert _run_state(runtime, run_id) == RunState.STARTED.value
    return execution


def leftover_operator_cancelled_behind_a_superseded_ordinal(
    runtime: DbosRuntime, run_id: RunId
) -> AgentAttemptExecution:
    """A foreign, superseded ordinal-1 sits beside the live, operator-cancelled ordinal-2.

    Ordinal 1 is cancelled under a foreign command with `replacement=ONE` and
    superseded; ordinal 2 -- the node's live attempt -- is the one the
    operator actually cancels, reverted to look leftover exactly like
    `leftover_operator_cancelled` above. `_attempt_family_target_state` must
    read the live ordinal's own command, not whichever of the two rows an
    unordered scan happens to return first.
    """

    first = armed_attempt(runtime, run_id)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)
    armed = store.load(first.attempt_id)
    replace_command = CancelAgentAttemptRequest(
        run_id,
        first.attempt_id,
        "foreign-replace-command",
        armed.state_version,
        AgentAttemptReplacement.ONE,
    )
    accepted = store.request_cancellation(replace_command)
    assert isinstance(accepted, AgentAttemptCancellationAccepted), accepted
    superseded = store.attest_cancellation_cleanup(
        replace_command,
        AgentAttemptCancellationDisposition.EXITED_BEFORE_SIGNAL,
        OWNER,
        GENERATION,
    )
    assert superseded.attempt.state is AgentAttemptState.CANCELLED
    assert superseded.replacement_attempt_id is not None
    replacement_id = superseded.replacement_attempt_id

    second = AgentAttemptExecution(first.request, replacement_id, 2)
    store.bind_watchdog(second, OWNER, GENERATION)
    store.claim(second)
    live = store.load(replacement_id)
    operator_command = CancelAgentAttemptRequest(
        run_id,
        replacement_id,
        RunCancelCommandId.for_key(f"operator-{run_id.value}").value,
        live.state_version,
        AgentAttemptReplacement.NONE,
    )
    accepted_operator = store.request_cancellation(operator_command)
    assert isinstance(accepted_operator, AgentAttemptCancellationAccepted), (
        accepted_operator
    )
    terminal = store.attest_cancellation_cleanup(
        operator_command,
        AgentAttemptCancellationDisposition.EXITED_BEFORE_SIGNAL,
        OWNER,
        GENERATION,
    )
    assert terminal.attempt.state is AgentAttemptState.CANCELLED
    assert _run_state(runtime, run_id) == RunState.CANCELLED.value
    with runtime.engine.connect() as connection:
        reverted = connection.execute(
            runs.update()
            .where(runs.c.run_id == run_id.value)
            .values(state=RunState.STARTED.value, terminal_hash=None)
        )
        assert reverted.rowcount == 1
        connection.commit()
    assert _run_state(runtime, run_id) == RunState.STARTED.value
    return second


def silent_successor(runtime: DbosRuntime, run_id: RunId) -> AgentAttemptExecution:
    execution = armed_attempt(runtime, run_id, reviewed_planning_document())
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)
    store.complete_success(
        execution, AgentExecutionResult(THE_ANSWER_THE_SCHEMA_ADMITS)
    )
    assert _run_state(runtime, run_id) == RunState.STARTED.value
    assert _current_node(runtime, run_id) == SUCCESSOR
    return execution


def unprepared_first_node(runtime: DbosRuntime, run_id: RunId) -> NodeExecutionId:
    """A run standing on its first node with no Attempt of its own, ever.

    The shape a Runner-lease node leaves the moment it hands its Attempt to the
    slot's queue: the node workflow has succeeded, nothing is prepared under the
    node, and no attempt anywhere in the run has succeeded either -- so whether
    this run is alive is a question only the slot workflow's own row can answer.
    """

    workflow = WorkflowRevision(planning_document())
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    created = DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        runtime.agent_executor_registry,
    ).start_published(
        StartPublishedRunRequestV2(run_id, workflow.revision_hash, _bindings(runtime))
    )
    assert isinstance(created, DurableRunCreated), created
    assert _run_state(runtime, run_id) == RunState.STARTED.value
    assert _current_node(runtime, run_id) == NODE
    return _current_node_execution_id(runtime, run_id)


def in_flight_action(runtime: DbosRuntime, run_id: RunId) -> EffectIntent:
    """A healthy V3 run standing on its Action node, driven by its effect.

    Every gap predicate holds -- the Agent predecessor SUCCEEDED, the Action
    node carries no attempt and never will -- yet nothing is lost: the run's
    next move is owed by `durable_effect`, enqueued under the running
    application version exactly as the Action node's own workflow enqueues it.
    """

    published = DbosCatalogStore(runtime.engine).publish_revision(OPEN_PR_OPERATION)
    assert isinstance(
        published, (PublishedRevisionCreated, PublishedRevisionExisting)
    ), published
    document = action_document()
    execution = armed_attempt(runtime, run_id, document)
    DbosAgentAttemptStore(
        runtime.engine, runtime.settings.application_version
    ).complete_success(execution, AgentExecutionResult(THE_ANSWER_THE_SCHEMA_ADMITS))
    assert _run_state(runtime, run_id) == RunState.STARTED.value
    assert _current_node(runtime, run_id) == ACTION
    return prepare_and_launch_graph_action(
        runtime.engine,
        runtime.settings,
        run_id,
        WorkflowRevisionHash(WorkflowRevision(document).revision_hash.value),
        runtime.effect_adapter_binding,
    )


def requestless_silent_successor(
    runtime: DbosRuntime, run_id: RunId
) -> AgentAttemptExecution:
    """The measured leftover: successor never prepared a durable request.

    A modern start writes every node's request in the start transaction.
    The live row did not — keep_node_receipt returns None, and that is the
    family. Production cannot delete a request, so the test drops the
    immutability trigger only long enough to forget the current node's row.
    """

    execution = silent_successor(runtime, run_id)
    _forget_current_node_request(runtime, run_id)
    assert _current_request(runtime, run_id) is None
    return execution


def seed_v1_interrupted(runtime: DbosRuntime, run_id: RunId) -> None:
    """A V1 STARTED row with an INTERRUPTED attempt on the current node.

    The frozen V1 wire cannot end as FAILED. Dropping the format filter lists
    this row; this test then dies.
    """

    revision = WorkflowRevision(V1_DOCUMENT)
    attempt_id = Sha256Hash.of(b"v1-interrupted-attempt")
    node_execution_id = Sha256Hash.of(b"v1-interrupted-execution")
    request_hash = Sha256Hash.of(b"v1-interrupted-request")
    with runtime.engine.connect() as connection:
        connection.execute(
            workflow_revisions.insert().values(
                revision_hash=revision.revision_hash.value,
                document=V1_DOCUMENT,
            )
        )
        connection.execute(
            runs.insert().values(
                run_id=run_id.value,
                bootstrap_workflow_id=f"bootstrap-{run_id.value}",
                revision_hash=revision.revision_hash.value,
                workflow_format_version=int(WorkflowFormatVersion.V1),
                current_node_id="agent",
                current_round_ordinal=FIRST_ROUND_ORDINAL,
                state=RunState.STARTED.value,
                state_version=0,
                last_event_sequence=0,
            )
        )
        connection.execute(
            agent_attempts.insert().values(
                attempt_id=attempt_id.value,
                node_execution_id=node_execution_id.value,
                request_hash=request_hash.value,
                executor_operational_identity="exact-operation",
                run_id=run_id.value,
                workflow_revision_hash=revision.revision_hash.value,
                node_id="agent",
                attempt_ordinal=1,
                state=AgentAttemptState.INTERRUPTED.value,
                state_version=2,
                process_phase=AgentAttemptProcessPhase.CLEANUP_ATTESTED.value,
                process_owner_id=OWNER.value,
                watchdog_generation_id=GENERATION.value,
                cancellation_command_id=STOP_AFTER_DRIVER_LOSS,
                cancellation_expected_state_version=1,
                replacement=AgentAttemptReplacement.NONE.value,
                redrive_state=AgentAttemptRedriveState.CLEANUP_ATTESTED.value,
                cancellation_disposition=(
                    AgentAttemptCancellationDisposition.OWNER_LOST_AFTER_PARENT_DEATH.value
                ),
                cancellation_workflow_id=f"cancel-{run_id.value}",
                runner_evidence_acceptance_phase=(
                    RunnerEvidenceAcceptancePhase.NONE.value
                ),
            )
        )
        connection.commit()


def _run_state(runtime: DbosRuntime, run_id: RunId) -> str:
    with runtime.engine.connect() as connection:
        return str(
            connection.scalar(
                sa.select(runs.c.state).where(runs.c.run_id == run_id.value)
            )
        )


def _current_node(runtime: DbosRuntime, run_id: RunId) -> str:
    with runtime.engine.connect() as connection:
        return str(
            connection.scalar(
                sa.select(runs.c.current_node_id).where(runs.c.run_id == run_id.value)
            )
        )


def _terminal_hash(runtime: DbosRuntime, run_id: RunId) -> str | None:
    with runtime.engine.connect() as connection:
        value = connection.scalar(
            sa.select(runs.c.terminal_hash).where(runs.c.run_id == run_id.value)
        )
    return None if value is None else str(value)


def _attempt_state(runtime: DbosRuntime, run_id: RunId) -> str:
    with runtime.engine.connect() as connection:
        return str(
            connection.scalar(
                sa.select(agent_attempts.c.state).where(
                    agent_attempts.c.run_id == run_id.value
                )
            )
        )


def _intent_state(runtime: DbosRuntime, run_id: RunId) -> str:
    with runtime.engine.connect() as connection:
        return str(
            connection.scalar(
                sa.select(effect_intents.c.state).where(
                    effect_intents.c.run_id == run_id.value
                )
            )
        )


def _end_workflow(runtime: DbosRuntime, workflow_id: str) -> None:
    """Leave the row DBOS leaves when a workflow raises instead of crashing."""

    with runtime.engine.begin() as connection:
        ended = connection.execute(
            sa.text(
                "UPDATE workflow_status SET status='ERROR' WHERE workflow_uuid=:id"
            ),
            {"id": workflow_id},
        )
        assert ended.rowcount == 1


def _event_kinds(runtime: DbosRuntime, run_id: RunId) -> tuple[str, ...]:
    with runtime.engine.connect() as connection:
        return tuple(
            connection.scalars(
                sa.select(run_events.c.event_kind)
                .where(run_events.c.run_id == run_id.value)
                .order_by(run_events.c.event_sequence)
            )
        )


def _last_command(runtime: DbosRuntime, run_id: RunId) -> str | None:
    with runtime.engine.connect() as connection:
        value = connection.scalar(
            sa.select(run_events.c.cancellation_command_id)
            .where(run_events.c.run_id == run_id.value)
            .order_by(run_events.c.event_sequence.desc())
            .limit(1)
        )
    return None if value is None else str(value)


def _record_node_workflow(
    runtime: DbosRuntime,
    workflow_id: str,
    *,
    status: str,
    application_version: str,
) -> None:
    """Leave one node-workflow row in the state a recovered executor would find.

    The durable runtime owns this table; a test that wants a workflow in a
    version or status only a crash or a deploy produces cannot reach that
    state by running one.
    """

    with runtime.engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO workflow_status "
                "(workflow_uuid, status, created_at, updated_at, priority, "
                "application_version) "
                "VALUES (:workflow_id, :status, 0, 0, 0, :application_version)"
            ),
            {
                "workflow_id": workflow_id,
                "status": status,
                "application_version": application_version,
            },
        )


def _current_receipt(runtime: DbosRuntime, run_id: RunId) -> tuple[str, str] | None:
    with runtime.engine.connect() as connection:
        record = (
            connection.execute(
                sa.select(
                    runs.c.revision_hash,
                    runs.c.current_node_id,
                    runs.c.current_round_ordinal,
                ).where(runs.c.run_id == run_id.value)
            )
            .mappings()
            .one()
        )
        stored = (
            connection.execute(
                sa.select(
                    node_receipts_v3.c.disposition, node_receipts_v3.c.reason
                ).where(
                    node_receipts_v3.c.node_execution_id
                    == NodeExecutionId.for_node(
                        run_id,
                        WorkflowRevisionHash(str(record["revision_hash"])),
                        str(record["current_node_id"]),
                        int(record["current_round_ordinal"]),
                    ).value
                )
            )
            .mappings()
            .one_or_none()
        )
    if stored is None:
        return None
    reason, _schema, _value = read_stored_node_receipt_reason(str(stored["reason"]))
    return str(stored["disposition"]), reason


def _current_node_execution_id(runtime: DbosRuntime, run_id: RunId) -> NodeExecutionId:
    with runtime.engine.connect() as connection:
        record = (
            connection.execute(
                sa.select(
                    runs.c.revision_hash,
                    runs.c.current_node_id,
                    runs.c.current_round_ordinal,
                ).where(runs.c.run_id == run_id.value)
            )
            .mappings()
            .one()
        )
    return NodeExecutionId.for_node(
        run_id,
        WorkflowRevisionHash(str(record["revision_hash"])),
        str(record["current_node_id"]),
        int(record["current_round_ordinal"]),
    )


def _current_request(runtime: DbosRuntime, run_id: RunId) -> str | None:
    with runtime.engine.connect() as connection:
        value = connection.scalar(
            sa.select(node_execution_requests_v3.c.request_hash).where(
                node_execution_requests_v3.c.node_execution_id
                == _current_node_execution_id(runtime, run_id).value
            )
        )
    return None if value is None else str(value)


def _forget_current_node_request(runtime: DbosRuntime, run_id: RunId) -> None:
    node_execution_id = _current_node_execution_id(runtime, run_id).value
    with runtime.engine.begin() as connection:
        connection.execute(sa.text("DROP TRIGGER node_execution_requests_v3_no_delete"))
        deleted = connection.execute(
            node_execution_requests_v3.delete().where(
                node_execution_requests_v3.c.node_execution_id == node_execution_id
            )
        )
        assert deleted.rowcount == 1
        connection.execute(
            sa.text(_PRODUCT_TRIGGERS["node_execution_requests_v3_no_delete"])
        )


@pytest.mark.proves("an-interrupted-uncontinuable-run-ends-under-driver-loss")
def test_an_interrupted_leftover_ends_as_failed_under_driver_loss(
    runtime: DbosRuntime,
) -> None:
    run_id = RunId("inventory/interrupted")
    leftover_interrupted(runtime, run_id)
    before_events = _event_kinds(runtime, run_id)

    ended = converge_uncontinuable_runs(
        DbosUncontinuableRunStore(runtime.engine, runtime.settings.application_version)
    )

    assert ended == (run_id,)
    assert _run_state(runtime, run_id) == RunState.FAILED.value
    assert _terminal_hash(runtime, run_id) is not None
    assert _attempt_state(runtime, run_id) == AgentAttemptState.INTERRUPTED.value
    assert _event_kinds(runtime, run_id) == before_events
    assert _last_command(runtime, run_id) == STOP_AFTER_DRIVER_LOSS
    assert before_events == (
        RunEventKind.AGENT_CANCEL_REQUESTED.value,
        RunEventKind.AGENT_INTERRUPTED.value,
    )


def test_an_operator_cancelled_leftover_ends_cancelled_not_failed(
    runtime: DbosRuntime,
) -> None:
    """#439 P3: the command's identity, not the driver-loss word, wins here too.

    The attempt reached `CANCELLED` under an operator run-cancel command, not
    `atelier2-driver-lost` -- `_attempt_family_target_state` reads that
    identity off the durable row and lifts this leftover to
    `RunState.CANCELLED`, the same word the in-transaction lift would have
    used had this row not been reverted to look leftover.
    """
    run_id = RunId("inventory/operator-cancelled")
    leftover_operator_cancelled(runtime, run_id)
    before_events = _event_kinds(runtime, run_id)

    ended = converge_uncontinuable_runs(
        DbosUncontinuableRunStore(runtime.engine, runtime.settings.application_version)
    )

    assert ended == (run_id,)
    assert _run_state(runtime, run_id) == RunState.CANCELLED.value
    assert _terminal_hash(runtime, run_id) is not None
    assert _attempt_state(runtime, run_id) == AgentAttemptState.CANCELLED.value
    assert _event_kinds(runtime, run_id) == before_events


def test_an_operator_cancelled_leftover_ends_cancelled_behind_a_superseded_ordinal(
    runtime: DbosRuntime,
) -> None:
    """A foreign, superseded ordinal-1 must not hide the live ordinal-2's word.

    `_attempt_family_target_state` orders its current-node lookup by
    `attempt_ordinal` descending: an unordered read could return the
    superseded ordinal-1's foreign command instead of the live ordinal-2's
    operator command, answering `None` (or `FAILED`) for a run the operator
    genuinely cancelled and leaving it `STARTED` forever.
    """
    run_id = RunId("inventory/operator-cancelled-superseded")
    execution = leftover_operator_cancelled_behind_a_superseded_ordinal(runtime, run_id)
    assert execution.ordinal == 2
    before_events = _event_kinds(runtime, run_id)

    ended = converge_uncontinuable_runs(
        DbosUncontinuableRunStore(runtime.engine, runtime.settings.application_version)
    )

    assert ended == (run_id,)
    assert _run_state(runtime, run_id) == RunState.CANCELLED.value
    assert _terminal_hash(runtime, run_id) is not None
    with runtime.engine.connect() as connection:
        live_state = connection.scalar(
            sa.select(agent_attempts.c.state).where(
                agent_attempts.c.attempt_id == execution.attempt_id.value
            )
        )
    assert live_state == AgentAttemptState.CANCELLED.value
    assert _event_kinds(runtime, run_id) == before_events


def test_converge_does_not_end_a_run_whose_cancelled_attempt_is_not_the_operators(
    runtime: DbosRuntime,
) -> None:
    """A `CANCELLED` attempt under a foreign command is not this net's to close.

    The attempt route (#15) can cancel with no replacement too, and reaches
    the exact same `CANCELLED` attempt state -- but its command id was never
    minted by `RunCancelCommandId.for_key`. `uncontinuable_runs()` lists the
    run anyway (its own `CANCELLED` arm is deliberately loose, precision is
    `end_uncontinuable_run`'s job); dropping the `is_operator_run_cancel`
    check there instead ends this run, and this test dies.
    """
    run_id = RunId("inventory/foreign-cancelled")
    execution = armed_attempt(runtime, run_id)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)
    armed = store.load(execution.attempt_id)
    command = CancelAgentAttemptRequest(
        run_id,
        execution.attempt_id,
        "attempt-route-command",
        armed.state_version,
        AgentAttemptReplacement.NONE,
    )
    accepted = store.request_cancellation(command)
    assert isinstance(accepted, AgentAttemptCancellationAccepted), accepted
    terminal = store.attest_cancellation_cleanup(
        command,
        AgentAttemptCancellationDisposition.EXITED_BEFORE_SIGNAL,
        OWNER,
        GENERATION,
    )
    assert terminal.attempt.state is AgentAttemptState.CANCELLED
    # Not the operator's command, so #439 P3's own lift never touched this run.
    assert _run_state(runtime, run_id) == RunState.STARTED.value

    inventory = DbosUncontinuableRunStore(
        runtime.engine, runtime.settings.application_version
    )
    assert inventory.uncontinuable_runs() == (run_id,)
    assert converge_uncontinuable_runs(inventory) == ()
    assert _run_state(runtime, run_id) == RunState.STARTED.value


def test_converge_does_not_end_an_interrupted_run_that_can_still_continue(
    runtime: DbosRuntime,
) -> None:
    """A replacement still in flight is not leftover inventory.

    Dropping the ``~sa.exists`` clause lists this row, and this test dies.
    """

    run_id = RunId("inventory/living")
    execution = leftover_interrupted(runtime, run_id)
    replacement = AgentAttemptId.for_execution(
        execution.request.node_execution_id,
        execution.request.request_hash,
        REPLACEMENT_AGENT_ATTEMPT_ORDINAL,
    )
    with runtime.engine.connect() as connection:
        connection.execute(
            agent_attempts.insert().values(
                attempt_id=replacement.value,
                node_execution_id=execution.request.node_execution_id.value,
                request_hash=execution.request.request_hash.value,
                executor_operational_identity=(
                    execution.request.executor_operational_identity.value
                ),
                run_id=run_id.value,
                workflow_revision_hash=execution.request.workflow_revision_hash.value,
                node_id=execution.request.node_id,
                attempt_ordinal=REPLACEMENT_AGENT_ATTEMPT_ORDINAL,
                state=AgentAttemptState.LAUNCH_ARMED.value,
                state_version=1,
                process_phase=AgentAttemptProcessPhase.NONE.value,
                runner_evidence_acceptance_phase=(
                    RunnerEvidenceAcceptancePhase.NONE.value
                ),
            )
        )
        connection.commit()

    store = DbosUncontinuableRunStore(
        runtime.engine, runtime.settings.application_version
    )
    assert store.uncontinuable_runs() == ()
    assert converge_uncontinuable_runs(store) == ()
    assert _run_state(runtime, run_id) == RunState.STARTED.value


def test_converge_does_not_end_an_interrupted_run_waiting_for_input(
    runtime: DbosRuntime,
) -> None:
    """A run waiting for a person is not leftover inventory.

    Dropping the STARTED filter lists this row, and this test dies.
    """

    run_id = RunId("inventory/waiting")
    leftover_interrupted(runtime, run_id)
    with runtime.engine.connect() as connection:
        waiting = connection.execute(
            runs.update()
            .where(runs.c.run_id == run_id.value)
            .values(state=RunState.WAITING_INPUT.value)
        )
        assert waiting.rowcount == 1
        connection.commit()

    store = DbosUncontinuableRunStore(
        runtime.engine, runtime.settings.application_version
    )
    assert store.uncontinuable_runs() == ()
    assert converge_uncontinuable_runs(store) == ()
    assert _run_state(runtime, run_id) == RunState.WAITING_INPUT.value


def test_converge_does_not_end_a_silent_successor(runtime: DbosRuntime) -> None:
    """A succeeded predecessor and no attempt on the current node is not named
    while the node workflow that would start that attempt is still owed a step.

    The window between Advance and `start_node` looks identical in the store.
    Inventing FAILED there would eat a living run. The live row
    `live/die-kette-sieht` is the other side of this pair: its workflow is
    gone, and that case is named below.
    """

    run_id = RunId("inventory/silent")
    execution = silent_successor(runtime, run_id)
    _record_node_workflow(
        runtime,
        node_workflow_id_for(execution.request.node_execution_id),
        status="PENDING",
        application_version=runtime.settings.application_version,
    )

    store = DbosUncontinuableRunStore(
        runtime.engine, runtime.settings.application_version
    )
    assert store.uncontinuable_runs() == ()
    assert converge_uncontinuable_runs(store) == ()
    assert _run_state(runtime, run_id) == RunState.STARTED.value
    assert _attempt_state(runtime, run_id) == AgentAttemptState.SUCCEEDED.value
    assert _current_receipt(runtime, run_id) is None


@pytest.mark.proves("a-serve-start-ends-a-run-that-died-between-nodes")
def test_converge_ends_a_silent_successor_whose_durable_workflow_is_gone(
    runtime: DbosRuntime,
) -> None:
    """The gap family: successor never prepared, and nothing will start it.

    After a deploy, DBOS recovers only the running application version.
    A missing node workflow, or one from a dead version, is that fact.
    """

    run_id = RunId("inventory/gap-missing")
    silent_successor(runtime, run_id)
    before_events = _event_kinds(runtime, run_id)

    ended = converge_uncontinuable_runs(
        DbosUncontinuableRunStore(runtime.engine, runtime.settings.application_version)
    )

    assert ended == (run_id,)
    assert _run_state(runtime, run_id) == RunState.FAILED.value
    assert _terminal_hash(runtime, run_id) is not None
    assert _attempt_state(runtime, run_id) == AgentAttemptState.SUCCEEDED.value
    assert _event_kinds(runtime, run_id) == before_events
    assert _current_receipt(runtime, run_id) == (
        PersistedReceiptDisposition.FAILED.value,
        STOP_AFTER_DRIVER_LOSS,
    )


@pytest.mark.proves("a-serve-start-ends-a-run-that-died-between-nodes")
def test_converge_ends_a_silent_successor_whose_durable_workflow_is_the_dead_version(
    runtime: DbosRuntime,
) -> None:
    """PENDING under a version that will never recover is still leftover inventory.

    Dropping the application_version match lists a living successor as dead,
    and the mirror test above dies.
    """

    run_id = RunId("inventory/gap-dead-version")
    execution = silent_successor(runtime, run_id)
    _record_node_workflow(
        runtime,
        node_workflow_id_for(execution.request.node_execution_id),
        status="PENDING",
        application_version="dead-application-version",
    )
    before_events = _event_kinds(runtime, run_id)

    ended = converge_uncontinuable_runs(
        DbosUncontinuableRunStore(runtime.engine, runtime.settings.application_version)
    )

    assert ended == (run_id,)
    assert _run_state(runtime, run_id) == RunState.FAILED.value
    assert _terminal_hash(runtime, run_id) is not None
    assert _event_kinds(runtime, run_id) == before_events
    assert _current_receipt(runtime, run_id) == (
        PersistedReceiptDisposition.FAILED.value,
        STOP_AFTER_DRIVER_LOSS,
    )


@pytest.mark.proves("a-serve-start-ends-a-run-that-died-between-nodes")
def test_converge_ends_a_silent_successor_that_never_received_a_request(
    runtime: DbosRuntime,
) -> None:
    """The measured leftover: no durable request on the current node.

    keep_node_receipt reads hashes from that row. Requiring a receipt
    here is requiring a request nobody wrote. Serve start still ends
    the run FAILED; the current node stays honestly receipt-less.
    """

    run_id = RunId("inventory/gap-requestless")
    requestless_silent_successor(runtime, run_id)
    before_events = _event_kinds(runtime, run_id)

    ended = converge_uncontinuable_runs(
        DbosUncontinuableRunStore(runtime.engine, runtime.settings.application_version)
    )

    assert ended == (run_id,)
    assert _run_state(runtime, run_id) == RunState.FAILED.value
    assert _terminal_hash(runtime, run_id) is not None
    assert _attempt_state(runtime, run_id) == AgentAttemptState.SUCCEEDED.value
    assert _event_kinds(runtime, run_id) == before_events
    assert _current_receipt(runtime, run_id) is None


def test_converge_does_not_end_a_run_whose_effect_workflow_is_still_driving(
    runtime: DbosRuntime,
) -> None:
    """#645: an Action node's driver is its effect workflow, not a node workflow.

    Every gap predicate holds for a healthy in-flight Action, so reading only
    node and replacement workflows names this living run dead and fails it --
    the FAILED mirror of the COMPLETED lie. Dropping the effect drivers from
    `_gap_workflow_ids` ends this run, and this test dies.
    """

    run_id = RunId("inventory/action-in-flight")
    in_flight_action(runtime, run_id)

    store = DbosUncontinuableRunStore(
        runtime.engine, runtime.settings.application_version
    )
    assert store.uncontinuable_runs() == ()
    assert converge_uncontinuable_runs(store) == ()
    assert _run_state(runtime, run_id) == RunState.STARTED.value
    assert _intent_state(runtime, run_id) == EffectIntentState.PREPARED.value
    assert _current_receipt(runtime, run_id) is None


def test_a_serve_start_routes_a_driverless_action_to_the_door_not_to_failed(
    runtime: DbosRuntime,
) -> None:
    """The Action whose effect workflow really raised still reaches the operator.

    #628 routes it to WAITING_RECONCILIATION before this inventory reads the
    STARTED rows, and #645's driver check must not shield it there instead:
    a raised `durable_effect` is not a live driver, so the run leaves STARTED
    through the operator door (ADR 0010) rather than through FAILED.
    """

    run_id = RunId("inventory/action-driverless")
    intent = in_flight_action(runtime, run_id)
    _end_workflow(runtime, effect_workflow_id_for(intent.binding.logical_key))

    runtime.launch()

    assert _run_state(runtime, run_id) == RunState.WAITING_RECONCILIATION.value
    assert (
        _intent_state(runtime, run_id) == EffectIntentState.WAITING_RECONCILIATION.value
    )
    assert _event_kinds(runtime, run_id) == (
        RunEventKind.AGENT_COMPLETED.value,
        RunEventKind.ACTION_RECONCILIATION_REQUIRED.value,
    )


def test_converge_does_not_end_a_v1_run(runtime: DbosRuntime) -> None:
    """The frozen V1 wire cannot end as FAILED.

    Dropping the format-version filter lists this row, and this test dies.
    """

    run_id = RunId("inventory/v1")
    seed_v1_interrupted(runtime, run_id)

    store = DbosUncontinuableRunStore(
        runtime.engine, runtime.settings.application_version
    )
    assert store.uncontinuable_runs() == ()
    assert converge_uncontinuable_runs(store) == ()
    assert _run_state(runtime, run_id) == RunState.STARTED.value


@pytest.mark.proves("a-serve-start-ends-interrupted-uncontinuable-inventory")
def test_a_serve_start_ends_the_recoverable_class_cases_and_leaves_the_protected(
    runtime: DbosRuntime,
) -> None:
    interrupted = RunId("inventory/interrupted")
    failed = RunId("inventory/failed")
    waiting = RunId("inventory/waiting")
    silent = RunId("inventory/silent")
    gap = RunId("inventory/gap")
    gap_requestless = RunId("inventory/gap-requestless")
    v1 = RunId("inventory/v1")
    leftover_interrupted(runtime, interrupted)
    leftover_failed(runtime, failed)
    leftover_interrupted(runtime, waiting)
    with runtime.engine.connect() as connection:
        connection.execute(
            runs.update()
            .where(runs.c.run_id == waiting.value)
            .values(state=RunState.WAITING_INPUT.value)
        )
        connection.commit()
    living = silent_successor(runtime, silent)
    _record_node_workflow(
        runtime,
        node_workflow_id_for(living.request.node_execution_id),
        status="PENDING",
        application_version=runtime.settings.application_version,
    )
    silent_successor(runtime, gap)
    requestless_silent_successor(runtime, gap_requestless)
    seed_v1_interrupted(runtime, v1)

    runtime.launch()

    assert _run_state(runtime, interrupted) == RunState.FAILED.value
    assert _run_state(runtime, failed) == RunState.STARTED.value
    assert _last_command(runtime, interrupted) == STOP_AFTER_DRIVER_LOSS
    assert _attempt_state(runtime, interrupted) == AgentAttemptState.INTERRUPTED.value
    assert _run_state(runtime, waiting) == RunState.WAITING_INPUT.value
    assert _run_state(runtime, silent) == RunState.STARTED.value
    assert _run_state(runtime, gap) == RunState.FAILED.value
    assert _current_receipt(runtime, gap) == (
        PersistedReceiptDisposition.FAILED.value,
        STOP_AFTER_DRIVER_LOSS,
    )
    assert _run_state(runtime, gap_requestless) == RunState.FAILED.value
    assert _current_receipt(runtime, gap_requestless) is None
    assert _run_state(runtime, v1) == RunState.STARTED.value
