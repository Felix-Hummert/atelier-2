from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from dbos import DBOSClient, EnqueueOptions
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DatabaseError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from atelier2.adapters.dbos.agent_catalog import (
    agent_configuration_from_record,
    auth_profile_from_record,
)
from atelier2.adapters.dbos.effect_store import (
    intent_snapshot_from_record,
    receipt_from_record,
)
from atelier2.adapters.dbos.schema import (
    agent_configuration_revisions,
    agent_receipts,
    auth_profile_revisions,
    effect_intents,
    effect_receipts,
    run_agent_bindings,
    run_events,
    runs,
    wait_answers,
    workflow_revisions,
)
from atelier2.adapters.yaml_workflows import parse_executable_workflow_document
from atelier2.contracts.agent_attempts import AgentAttemptId
from atelier2.contracts.agents import (
    AgentBindingSetHash,
    AgentConfigurationRevisionHash,
    AgentExecutionRequest,
    AgentExecutionRequestHash,
    AgentExecutionResult,
    AgentExecutorBinding,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    AgentOutputHash,
    AgentReceipt,
    AgentReceiptHash,
    AgentReceiptV2,
    AgentRole,
    AuthMode,
    AuthProfileRevisionHash,
    ProviderId,
    ResolvedAgentBinding,
)
from atelier2.contracts.effects import LogicalEffectKey
from atelier2.contracts.executions import (
    NodeExecutionId,
    RunEvent,
    RunEventKind,
    SubmitWaitAnswerRequest,
    TransitionSnapshot,
    WaitAnswer,
    WaitAnswerSnapshot,
    WaitAnswerState,
    is_canonical_integer_bytes,
    logical_effect_key_for,
    terminal_hash_for,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.run_bindings import AnyRun, RunV2, RunV3
from atelier2.contracts.run_configuration_v3 import RunConfigurationRevisionHash
from atelier2.contracts.runs import (
    RevisionHashCollision,
    Run,
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.contracts.workflows import (
    ActionNode,
    AgentNodeV2,
    WaitNode,
    WorkflowGraphV2,
)
from atelier2.contracts.workflows_v3 import (
    AgentNodeV3,
    AnyWorkflowDocument,
    WorkflowGraphV3,
    is_sink_node,
)
from atelier2.ports.durable_runs import (
    DurableAnswerBytesConflict,
    DurableAnswerCreated,
    DurableAnswerExisting,
    DurableAnswerNodeMissing,
    DurableAnswerResult,
    DurableAnswerRevisionConflict,
    DurableAnswerRunMissing,
    DurableAnswerStateConflict,
    DurableStateCorrupt,
    DurableWriteUnavailable,
)


class RunTransitionConflict(RuntimeError):
    """A retry or transition contradicts the exact durable graph/run/event binding."""


class AgentReceiptConflict(RunTransitionConflict):
    """One stable node execution contradicts its durable agent receipt."""


def load_graph(
    session: Any, revision_hash: WorkflowRevisionHash
) -> AnyWorkflowDocument:
    document = session.scalar(
        sa.select(workflow_revisions.c.document).where(
            workflow_revisions.c.revision_hash == revision_hash.value
        )
    )
    if document is None:
        raise RunTransitionConflict("workflow revision is missing")
    return graph_from_document(revision_hash, bytes(document))


def graph_from_document(
    revision_hash: WorkflowRevisionHash, document: bytes
) -> AnyWorkflowDocument:
    revision = WorkflowRevision(bytes(document))
    if revision.revision_hash != revision_hash:
        raise RevisionHashCollision(
            "durable workflow revision bytes disagree with their hash"
        )
    return parse_executable_workflow_document(revision.document)


def run_from_record(record: Mapping[Any, Any]) -> Run:
    if (
        int(record["workflow_format_version"]) != 1
        or record["agent_binding_set_hash"] is not None
    ):
        raise RunTransitionConflict("V1 run record carries a V2 binding")
    terminal = record["terminal_hash"]
    return Run(
        RunId(str(record["run_id"])),
        WorkflowRevisionHash(str(record["revision_hash"])),
        RunState(str(record["state"])),
        str(record["current_node_id"]),
        int(record["state_version"]),
        int(record["last_event_sequence"]),
        None if terminal is None else Sha256Hash(str(terminal)),
    )


def run_from_record_with_bindings(session: Any, record: Mapping[Any, Any]) -> AnyRun:
    version = int(record["workflow_format_version"])
    if version == 1:
        return run_from_record(record)
    # A V3 run binds its agent roles exactly as a V2 run does, so the rows below
    # are read the same way; what differs about V3 lives in the graph, not here.
    if version not in (2, 3) or record["agent_binding_set_hash"] is None:
        raise RunTransitionConflict("run format version and binding set disagree")
    run_id = RunId(str(record["run_id"]))
    revision_hash = WorkflowRevisionHash(str(record["revision_hash"]))
    binding_set_hash = AgentBindingSetHash(str(record["agent_binding_set_hash"]))
    rows = tuple(
        session.execute(
            sa.select(
                run_agent_bindings.c.revision_hash.label("run_revision_hash"),
                run_agent_bindings.c.binding_set_hash,
                run_agent_bindings.c.role,
                run_agent_bindings.c.agent_configuration_revision_hash,
                agent_configuration_revisions.c.revision_hash.label(
                    "configuration_revision_hash"
                ),
                agent_configuration_revisions.c.model,
                agent_configuration_revisions.c.auth_profile_revision_hash,
                agent_configuration_revisions.c.executor_revision,
                agent_configuration_revisions.c.revision_format_version,
                agent_configuration_revisions.c.requested_capability,
                auth_profile_revisions.c.revision_hash.label("auth_revision_hash"),
                auth_profile_revisions.c.profile_id,
                auth_profile_revisions.c.revision_number,
                auth_profile_revisions.c.provider_id,
                auth_profile_revisions.c.auth_mode,
            )
            .join(
                agent_configuration_revisions,
                agent_configuration_revisions.c.revision_hash
                == run_agent_bindings.c.agent_configuration_revision_hash,
            )
            .join(
                auth_profile_revisions,
                auth_profile_revisions.c.revision_hash
                == agent_configuration_revisions.c.auth_profile_revision_hash,
            )
            .where(run_agent_bindings.c.run_id == run_id.value)
            .order_by(sa.cast(run_agent_bindings.c.role, sa.LargeBinary()))
        ).mappings()
    )
    resolved: list[ResolvedAgentBinding] = []
    for row in rows:
        if (
            str(row["run_revision_hash"]) != revision_hash.value
            or str(row["binding_set_hash"]) != binding_set_hash.value
        ):
            raise RunTransitionConflict("run agent binding row disagrees with run")
        configuration = agent_configuration_from_record(
            {
                "revision_hash": row["configuration_revision_hash"],
                "model": row["model"],
                "auth_profile_revision_hash": row["auth_profile_revision_hash"],
                "executor_revision": row["executor_revision"],
                "revision_format_version": row["revision_format_version"],
                "requested_capability": row["requested_capability"],
            }
        )
        auth = auth_profile_from_record(
            {
                "revision_hash": row["auth_revision_hash"],
                "profile_id": row["profile_id"],
                "revision_number": row["revision_number"],
                "provider_id": row["provider_id"],
                "auth_mode": row["auth_mode"],
            }
        )
        if (
            str(row["agent_configuration_revision_hash"])
            != configuration.revision_hash.value
        ):
            raise RunTransitionConflict("run agent configuration binding disagrees")
        resolved.append(
            ResolvedAgentBinding(AgentRole(str(row["role"])), configuration, auth)
        )
    terminal = record["terminal_hash"]
    # A V3 row is read back as a V3 run, not as a V2 one wearing a new number:
    # the two are different truths, and a shared shape would mean every V2 reader
    # had silently been reading V3 rows all along.
    head = (
        run_id,
        revision_hash,
        binding_set_hash,
        tuple(resolved),
        RunState(str(record["state"])),
        str(record["current_node_id"]),
        int(record["state_version"]),
        int(record["last_event_sequence"]),
    )
    terminal_hash = None if terminal is None else Sha256Hash(str(terminal))
    if version == 2:
        return RunV2(*head, terminal_hash)
    configuration = record["run_configuration_revision_hash"]
    if configuration is None:
        raise RunTransitionConflict(
            "a format-3 run row carries no run configuration revision"
        )
    return RunV3(
        *head,
        RunConfigurationRevisionHash(str(configuration)),
        terminal_hash,
    )


def load_run(session: Any, run_id: RunId) -> AnyRun:
    record = (
        session.execute(sa.select(runs).where(runs.c.run_id == run_id.value))
        .mappings()
        .one_or_none()
    )
    if record is None:
        raise RunTransitionConflict("run does not exist")
    run = run_from_record_with_bindings(session, record)
    graph = load_graph(session, run.revision_hash)
    validate_run_graph_binding(run, graph)
    return run


def entry_node_of(graph: AnyWorkflowDocument) -> str:
    """Where a run of this document begins.

    V1 and V2 name it directly; a V3 graph derives its entry set from the nodes
    that depend on nothing. Only a single-entry V3 document starts today — a
    fan-out start needs the ready set ADR 0006 hands the scheduler, and refusing
    it here is what keeps this head from implying one.
    """
    if isinstance(graph, WorkflowGraphV3):
        entry = graph.entry_node_ids
        if len(entry) != 1:
            raise RunTransitionConflict(
                f"a V3 run starts at exactly one entry node, not {len(entry)}"
            )
        return entry[0]
    return graph.start


def successor_of(graph: AnyWorkflowDocument, node_id: str) -> str:
    """The one node a finished node hands the run to.

    V1 and V2 name it directly. A V3 run cannot reach this at all today: the one
    executable V3 shape is a single node, and nothing advances it, because
    advancing needs the ready set over `depends_on` that ADR 0006 hands the
    scheduler. Refused by name rather than answered by a rule that would be the
    first inch of that ready set -- H2 and #86 own it, and owning it here would
    mean deciding fan-out and join semantics in a head about a binding arm.
    """
    if isinstance(graph, WorkflowGraphV3):
        raise RunTransitionConflict(
            f"a V3 run has no advance path yet, so node {node_id!r} hands on to nothing"
        )
    return graph.successor(node_id).id


def validate_run_graph_binding(run: AnyRun, graph: AnyWorkflowDocument) -> None:
    # A V3 graph binds roles the way a V2 one does and is carried by the same run
    # shape, so both are the bound side of this check; only V1 has no bindings.
    bound_graph = isinstance(graph, (WorkflowGraphV2, WorkflowGraphV3))
    bound_run = isinstance(run, (RunV2, RunV3))
    if bound_run != bound_graph:
        raise RunTransitionConflict("run version differs from workflow graph version")
    if isinstance(run, RunV3) != isinstance(graph, WorkflowGraphV3):
        raise RunTransitionConflict("run format differs from workflow graph format")
    if isinstance(run, (RunV2, RunV3)):
        expected_roles = {
            node.role
            for node in graph.nodes
            if isinstance(node, (AgentNodeV2, AgentNodeV3))
        }
        if expected_roles != {binding.role.value for binding in run.agent_bindings}:
            raise RunTransitionConflict("run agent roles differ from workflow graph")
    node = graph.node(run.current_node_id)
    if run.state is RunState.WAITING_INPUT and not isinstance(node, WaitNode):
        raise RunTransitionConflict("WAITING_INPUT must name a Wait node")
    if run.state is RunState.WAITING_RECONCILIATION and not isinstance(
        node, ActionNode
    ):
        raise RunTransitionConflict("WAITING_RECONCILIATION must name an Action node")
    if run.state is RunState.COMPLETED and not is_sink_node(graph, run.current_node_id):
        raise RunTransitionConflict("COMPLETED must name the run's sink node")


def event_from_record(record: Mapping[Any, Any]) -> RunEvent:
    logical_key = record["receipt_logical_key"]
    result_hash = record["receipt_result_hash"]
    event = RunEvent(
        RunId(str(record["run_id"])),
        WorkflowRevisionHash(str(record["revision_hash"])),
        int(record["event_sequence"]),
        str(record["node_id"]),
        NodeExecutionId(str(record["node_execution_id"])),
        RunEventKind(str(record["event_kind"])),
        bytes(record["payload"]),
        None if logical_key is None else LogicalEffectKey(str(logical_key)),
        None if result_hash is None else Sha256Hash(str(result_hash)),
        None if record["agent_attempt_id"] is None else str(record["agent_attempt_id"]),
        None if record["attempt_ordinal"] is None else int(record["attempt_ordinal"]),
        None
        if record["cancellation_command_id"] is None
        else str(record["cancellation_command_id"]),
        None if record["replacement"] is None else str(record["replacement"]),
        None
        if record["cancellation_disposition"] is None
        else str(record["cancellation_disposition"]),
        None
        if record["replacement_attempt_id"] is None
        else str(record["replacement_attempt_id"]),
    )
    if event.payload_hash.value != record["payload_hash"]:
        raise RunTransitionConflict("durable event payload hash disagrees")
    if event.event_hash.value != record["event_hash"]:
        raise RunTransitionConflict("durable event hash disagrees")
    return event


def _existing_event(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    event_kind: RunEventKind,
    payload: bytes,
    receipt_logical_key: LogicalEffectKey | None,
    receipt_result_hash: Sha256Hash | None,
    agent_attempt_id: AgentAttemptId | None = None,
) -> TransitionSnapshot | None:
    record = (
        session.execute(
            sa.select(run_events).where(
                run_events.c.run_id == run_id.value,
                run_events.c.revision_hash == revision_hash.value,
                run_events.c.node_id == node_id,
                run_events.c.event_kind == event_kind.value,
                (
                    run_events.c.agent_attempt_id.is_(None)
                    if agent_attempt_id is None
                    else run_events.c.agent_attempt_id == agent_attempt_id.value
                ),
            )
        )
        .mappings()
        .one_or_none()
    )
    if record is None:
        return None
    event = event_from_record(record)
    expected_execution = NodeExecutionId.for_node(run_id, revision_hash, node_id)
    if (
        event.node_execution_id != expected_execution
        or event.payload != payload
        or event.receipt_logical_key != receipt_logical_key
        or event.receipt_result_hash != receipt_result_hash
    ):
        raise RunTransitionConflict("existing event differs from exact retry")
    current = load_run(session, run_id)
    if current.revision_hash != revision_hash:
        raise RunTransitionConflict("event retry names another run revision")
    return TransitionSnapshot(
        current.run_id,
        current.revision_hash,
        current.current_node_id,
        current.state,
        current.state_version,
        current.last_event_sequence,
        event,
    )


def _insert_event(session: Any, event: RunEvent) -> None:
    session.execute(
        run_events.insert().values(
            run_id=event.run_id.value,
            revision_hash=event.revision_hash.value,
            event_sequence=event.event_sequence,
            node_id=event.node_id,
            node_execution_id=event.node_execution_id.value,
            event_kind=event.event_kind.value,
            payload=event.payload,
            payload_hash=event.payload_hash.value,
            receipt_logical_key=(
                None
                if event.receipt_logical_key is None
                else event.receipt_logical_key.value
            ),
            receipt_result_hash=(
                None
                if event.receipt_result_hash is None
                else event.receipt_result_hash.value
            ),
            event_hash=event.event_hash.value,
            agent_attempt_id=event.agent_attempt_id,
            attempt_ordinal=event.attempt_ordinal,
            cancellation_command_id=event.cancellation_command_id,
            replacement=event.replacement,
            cancellation_disposition=event.cancellation_disposition,
            replacement_attempt_id=event.replacement_attempt_id,
        )
    )


def _commit_event(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    event_kind: RunEventKind,
    payload: bytes,
    expected_state: RunState,
    target_state: RunState,
    target_node_id: str,
    receipt_logical_key: LogicalEffectKey | None = None,
    receipt_result_hash: Sha256Hash | None = None,
    terminal: bool = False,
    agent_attempt_id: AgentAttemptId | None = None,
    attempt_ordinal: int | None = None,
) -> TransitionSnapshot:
    existing = _existing_event(
        session,
        run_id,
        revision_hash,
        node_id,
        event_kind,
        payload,
        receipt_logical_key,
        receipt_result_hash,
        agent_attempt_id,
    )
    if existing is not None:
        return existing
    current = load_run(session, run_id)
    if (
        current.revision_hash != revision_hash
        or current.current_node_id != node_id
        or current.state is not expected_state
    ):
        raise RunTransitionConflict("run is not at the transition's exact source")
    graph = load_graph(session, revision_hash)
    target_node = graph.node(target_node_id)
    if target_state is RunState.WAITING_INPUT and not isinstance(target_node, WaitNode):
        raise RunTransitionConflict("WAITING_INPUT target is not a Wait node")
    if target_state is RunState.WAITING_RECONCILIATION and not isinstance(
        target_node, ActionNode
    ):
        raise RunTransitionConflict("WAITING_RECONCILIATION target is not an Action")
    if terminal != (target_state is RunState.COMPLETED):
        raise RunTransitionConflict("terminal transition shape disagrees")
    if terminal and not is_sink_node(graph, target_node_id):
        raise RunTransitionConflict("terminal transition must finish the run's sink")
    sequence = current.last_event_sequence + 1
    event = RunEvent(
        run_id,
        revision_hash,
        sequence,
        node_id,
        NodeExecutionId.for_node(run_id, revision_hash, node_id),
        event_kind,
        payload,
        receipt_logical_key,
        receipt_result_hash,
        None if agent_attempt_id is None else agent_attempt_id.value,
        attempt_ordinal,
    )
    terminal_hash: Sha256Hash | None = None
    if terminal:
        _insert_event(session, event)
        prior_hashes = tuple(
            Sha256Hash(str(value))
            for value in session.execute(
                sa.select(run_events.c.event_hash)
                .where(run_events.c.run_id == run_id.value)
                .order_by(run_events.c.event_sequence)
            ).scalars()
        )
        terminal_hash = terminal_hash_for(revision_hash, prior_hashes)
    updated = session.execute(
        runs.update()
        .where(
            runs.c.run_id == run_id.value,
            runs.c.revision_hash == revision_hash.value,
            runs.c.current_node_id == node_id,
            runs.c.state == expected_state.value,
            runs.c.state_version == current.state_version,
            runs.c.last_event_sequence == current.last_event_sequence,
        )
        .values(
            current_node_id=target_node_id,
            state=target_state.value,
            state_version=current.state_version + 1,
            last_event_sequence=sequence,
            terminal_hash=None if terminal_hash is None else terminal_hash.value,
        )
    )
    if updated.rowcount != 1:
        raise RunTransitionConflict("run transition lost its state/version CAS")
    if not terminal:
        _insert_event(session, event)
    return TransitionSnapshot(
        run_id,
        revision_hash,
        target_node_id,
        target_state,
        current.state_version + 1,
        sequence,
        event,
    )


def _agent_receipt_values(receipt: AgentReceipt) -> dict[str, object]:
    return {
        "node_execution_id": receipt.node_execution_id.value,
        "request_hash": receipt.request_hash.value,
        "run_id": receipt.run_id.value,
        "workflow_revision_hash": receipt.workflow_revision_hash.value,
        "node_id": receipt.node_id,
        "executor_adapter_revision": (receipt.executor_binding.adapter_revision.value),
        "executor_operational_identity": (
            receipt.executor_binding.operational_identity.value
        ),
        "output_bytes": receipt.output_bytes,
        "output_hash": receipt.output_hash.value,
        "receipt_hash": receipt.receipt_hash.value,
    }


def _agent_receipt_from_record(record: Mapping[Any, Any]) -> AgentReceipt:
    try:
        return AgentReceipt(
            AgentExecutionRequestHash(str(record["request_hash"])),
            NodeExecutionId(str(record["node_execution_id"])),
            RunId(str(record["run_id"])),
            WorkflowRevisionHash(str(record["workflow_revision_hash"])),
            str(record["node_id"]),
            AgentExecutorBinding(
                AgentExecutorRevision(str(record["executor_adapter_revision"])),
                AgentExecutorOperationalIdentity(
                    str(record["executor_operational_identity"])
                ),
            ),
            bytes(record["output_bytes"]),
            AgentOutputHash(str(record["output_hash"])),
            AgentReceiptHash(str(record["receipt_hash"])),
        )
    except ValueError as error:
        raise AgentReceiptConflict(
            "durable agent receipt hash binding disagrees"
        ) from error


def commit_agent_completed(
    session: Any,
    request: AgentExecutionRequest,
    executor_binding: AgentExecutorBinding,
    result: AgentExecutionResult,
) -> TransitionSnapshot:
    graph = load_graph(session, request.workflow_revision_hash)
    receipt = AgentReceipt.for_execution(request, executor_binding, result)
    session.execute(
        agent_receipts.insert()
        .prefix_with("OR IGNORE")
        .values(_agent_receipt_values(receipt))
    )
    durable_record = (
        session.execute(
            sa.select(agent_receipts).where(
                agent_receipts.c.node_execution_id == request.node_execution_id.value
            )
        )
        .mappings()
        .one()
    )
    if _agent_receipt_from_record(durable_record) != receipt:
        raise AgentReceiptConflict("durable agent receipt differs from exact retry")
    return _commit_event(
        session,
        request.run_id,
        request.workflow_revision_hash,
        request.node_id,
        RunEventKind.AGENT_COMPLETED,
        result.output_bytes,
        RunState.STARTED,
        RunState.STARTED,
        successor_of(graph, request.node_id),
    )


def _agent_receipt_v2_values(receipt: AgentReceiptV2) -> dict[str, object]:
    return {
        "node_execution_id": receipt.node_execution_id.value,
        "request_hash": receipt.request_hash.value,
        "run_id": receipt.run_id.value,
        "workflow_revision_hash": receipt.workflow_revision_hash.value,
        "node_id": receipt.node_id,
        "role": receipt.role.value,
        "binding_set_hash": receipt.binding_set_hash.value,
        "agent_configuration_revision_hash": (
            receipt.agent_configuration_revision_hash.value
        ),
        "auth_profile_revision_hash": receipt.auth_profile_revision_hash.value,
        "profile_id": receipt.profile_id,
        "revision_number": receipt.revision_number,
        "provider_id": receipt.provider_id.value,
        "auth_mode": receipt.auth_mode.value,
        "model": receipt.model,
        "executor_revision": receipt.executor_revision.value,
        "executor_operational_identity": (receipt.executor_operational_identity.value),
        "output_bytes": receipt.output_bytes,
        "output_hash": receipt.output_hash.value,
        "receipt_hash": receipt.receipt_hash.value,
    }


def _agent_receipt_v2_from_record(record: Mapping[Any, Any]) -> AgentReceiptV2:
    try:
        return AgentReceiptV2(
            AgentExecutionRequestHash(str(record["request_hash"])),
            NodeExecutionId(str(record["node_execution_id"])),
            RunId(str(record["run_id"])),
            WorkflowRevisionHash(str(record["workflow_revision_hash"])),
            str(record["node_id"]),
            AgentRole(str(record["role"])),
            AgentBindingSetHash(str(record["binding_set_hash"])),
            AgentConfigurationRevisionHash(
                str(record["agent_configuration_revision_hash"])
            ),
            AuthProfileRevisionHash(str(record["auth_profile_revision_hash"])),
            str(record["profile_id"]),
            int(record["revision_number"]),
            ProviderId(str(record["provider_id"])),
            AuthMode(str(record["auth_mode"])),
            str(record["model"]),
            AgentExecutorRevision(str(record["executor_revision"])),
            AgentExecutorOperationalIdentity(
                str(record["executor_operational_identity"])
            ),
            bytes(record["output_bytes"]),
            AgentOutputHash(str(record["output_hash"])),
            AgentReceiptHash(str(record["receipt_hash"])),
        )
    except ValueError as error:
        raise AgentReceiptConflict(
            "durable V2 agent receipt hash binding disagrees"
        ) from error


def commit_waiting_input(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
) -> TransitionSnapshot:
    return _commit_event(
        session,
        run_id,
        revision_hash,
        node_id,
        RunEventKind.WAITING_INPUT,
        b"",
        RunState.STARTED,
        RunState.WAITING_INPUT,
        node_id,
    )


def commit_reconciliation_required(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    request: bytes,
) -> TransitionSnapshot:
    return _commit_event(
        session,
        run_id,
        revision_hash,
        node_id,
        RunEventKind.ACTION_RECONCILIATION_REQUIRED,
        request,
        RunState.STARTED,
        RunState.WAITING_RECONCILIATION,
        node_id,
    )


def commit_reconciliation_resolved(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    logical_key: LogicalEffectKey,
    result: bytes,
    result_hash: Sha256Hash,
) -> TransitionSnapshot:
    return _commit_event(
        session,
        run_id,
        revision_hash,
        node_id,
        RunEventKind.ACTION_RECONCILIATION_RESOLVED,
        result,
        RunState.WAITING_RECONCILIATION,
        RunState.STARTED,
        node_id,
        logical_key,
        result_hash,
    )


def commit_action_completed(
    session: Any, logical_key: LogicalEffectKey, revision_hash: WorkflowRevisionHash
) -> TransitionSnapshot:
    intent_record = (
        session.execute(
            sa.select(effect_intents).where(
                effect_intents.c.logical_key == logical_key.value
            )
        )
        .mappings()
        .one_or_none()
    )
    receipt_record = (
        session.execute(
            sa.select(effect_receipts).where(
                effect_receipts.c.logical_key == logical_key.value
            )
        )
        .mappings()
        .one_or_none()
    )
    if intent_record is None or receipt_record is None:
        raise RunTransitionConflict("confirmed Action requires its intent and receipt")
    intent = intent_snapshot_from_record(intent_record).intent
    receipt = receipt_from_record(receipt_record)
    run_id = intent.binding.run_id
    graph = load_graph(session, revision_hash)
    actions = [node for node in graph.nodes if isinstance(node, ActionNode)]
    if len(actions) != 1:
        raise RunTransitionConflict("confirmed intent graph has no single Action")
    action = actions[0]
    execution_id = NodeExecutionId.for_node(run_id, revision_hash, action.id)
    if (
        not isinstance(action, ActionNode)
        or logical_key != logical_effect_key_for(execution_id)
        or intent.binding.workflow_revision_hash != revision_hash
        or receipt.intent != intent
    ):
        raise RunTransitionConflict("logical effect key does not own current Action")
    return _commit_event(
        session,
        run_id,
        revision_hash,
        action.id,
        RunEventKind.ACTION_COMPLETED,
        receipt.result.payload,
        RunState.STARTED,
        RunState.STARTED,
        successor_of(graph, action.id),
        logical_key,
        receipt.result.payload_hash,
    )


def commit_wait_answered(session: Any, answer: WaitAnswer) -> TransitionSnapshot:
    record = (
        session.execute(
            sa.select(wait_answers).where(
                wait_answers.c.run_id == answer.run_id.value,
                wait_answers.c.node_id == answer.node_id,
            )
        )
        .mappings()
        .one_or_none()
    )
    if record is None:
        raise RunTransitionConflict("answer workflow has no durable answer")
    durable = wait_answer_snapshot_from_record(record)
    if durable.answer != answer:
        raise RunTransitionConflict("answer workflow binding differs")
    graph = load_graph(session, answer.revision_hash)
    transition = _commit_event(
        session,
        answer.run_id,
        answer.revision_hash,
        answer.node_id,
        RunEventKind.WAIT_ANSWERED,
        answer.answer_bytes,
        RunState.WAITING_INPUT,
        RunState.STARTED,
        successor_of(graph, answer.node_id),
    )
    if durable.state is WaitAnswerState.PENDING:
        updated = session.execute(
            wait_answers.update()
            .where(
                wait_answers.c.run_id == answer.run_id.value,
                wait_answers.c.node_id == answer.node_id,
                wait_answers.c.state == WaitAnswerState.PENDING.value,
                wait_answers.c.state_version == 0,
            )
            .values(state=WaitAnswerState.APPLIED.value, state_version=1)
        )
        if updated.rowcount != 1:
            raise RunTransitionConflict("answer apply lost its state CAS")
    elif transition.event.event_kind is not RunEventKind.WAIT_ANSWERED:
        raise RunTransitionConflict("applied answer has no exact event")
    return transition


def commit_subworkflow_completed(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    result: int,
) -> TransitionSnapshot:
    payload = str(result).encode("ascii")
    return _commit_event(
        session,
        run_id,
        revision_hash,
        node_id,
        RunEventKind.SUBWORKFLOW_COMPLETED,
        payload,
        RunState.STARTED,
        RunState.COMPLETED,
        node_id,
        terminal=True,
    )


def wait_answer_snapshot_from_record(record: Mapping[Any, Any]) -> WaitAnswerSnapshot:
    answer = WaitAnswer(
        RunId(str(record["run_id"])),
        WorkflowRevisionHash(str(record["revision_hash"])),
        str(record["node_id"]),
        NodeExecutionId(str(record["node_execution_id"])),
        bytes(record["answer_bytes"]),
    )
    if (
        answer.answer_hash.value != record["answer_hash"]
        or answer.answer_workflow_id != record["answer_workflow_id"]
    ):
        raise RunTransitionConflict("durable wait answer hashes or identity disagree")
    return WaitAnswerSnapshot(
        answer, WaitAnswerState(str(record["state"])), int(record["state_version"])
    )


def load_wait_answer(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
) -> WaitAnswerSnapshot:
    record = (
        session.execute(
            sa.select(wait_answers).where(
                wait_answers.c.run_id == run_id.value,
                wait_answers.c.revision_hash == revision_hash.value,
                wait_answers.c.node_id == node_id,
            )
        )
        .mappings()
        .one_or_none()
    )
    if record is None:
        raise RunTransitionConflict("wait answer does not exist")
    return wait_answer_snapshot_from_record(record)


class DbosWaitAnswerer:
    def __init__(self, engine: Engine, application_version: str) -> None:
        self._engine = engine
        self._application_version = application_version

    def submit_result(self, request: SubmitWaitAnswerRequest) -> DurableAnswerResult:
        if not is_canonical_integer_bytes(request.answer_bytes):
            return DurableAnswerStateConflict()
        from atelier2.adapters.dbos.workflow import ANSWER_WORKFLOW_NAME, QUEUE_NAME

        try:
            with self._engine.connect() as read_connection:
                document = read_connection.scalar(
                    sa.select(workflow_revisions.c.document).where(
                        workflow_revisions.c.revision_hash
                        == request.revision_hash.value
                    )
                )
            if document is None:
                prepared_document = None
                graph = None
            else:
                prepared_document = bytes(document)
                graph = graph_from_document(request.revision_hash, prepared_document)
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

        client: DBOSClient | None = None
        try:
            client = DBOSClient(
                system_database_engine=self._engine, use_listen_notify=False
            )
            with self._engine.connect() as connection:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                try:
                    run_record = (
                        connection.execute(
                            sa.select(runs).where(runs.c.run_id == request.run_id.value)
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if run_record is None:
                        connection.rollback()
                        return DurableAnswerRunMissing()
                    run = run_from_record_with_bindings(connection, run_record)
                    if run.revision_hash != request.revision_hash:
                        connection.rollback()
                        return DurableAnswerRevisionConflict()
                    stored_document = connection.scalar(
                        sa.select(workflow_revisions.c.document).where(
                            workflow_revisions.c.revision_hash
                            == request.revision_hash.value
                        )
                    )
                    if (
                        prepared_document is None
                        or graph is None
                        or stored_document is None
                        or bytes(stored_document) != prepared_document
                    ):
                        connection.rollback()
                        return DurableStateCorrupt()
                    stored_revision = WorkflowRevision(bytes(stored_document))
                    if stored_revision.revision_hash != request.revision_hash:
                        connection.rollback()
                        return DurableStateCorrupt()
                    execution_id = NodeExecutionId.for_node(
                        request.run_id, request.revision_hash, request.node_id
                    )
                    answer = WaitAnswer(
                        request.run_id,
                        request.revision_hash,
                        request.node_id,
                        execution_id,
                        request.answer_bytes,
                    )
                    inserted = connection.execute(
                        wait_answers.insert()
                        .prefix_with("OR IGNORE")
                        .values(
                            run_id=answer.run_id.value,
                            revision_hash=answer.revision_hash.value,
                            node_id=answer.node_id,
                            node_execution_id=answer.node_execution_id.value,
                            answer_bytes=answer.answer_bytes,
                            answer_hash=answer.answer_hash.value,
                            answer_workflow_id=answer.answer_workflow_id,
                            state=WaitAnswerState.PENDING.value,
                            state_version=0,
                        )
                    )
                    stored_record = (
                        connection.execute(
                            sa.select(wait_answers).where(
                                wait_answers.c.run_id == request.run_id.value,
                                wait_answers.c.node_id == request.node_id,
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if stored_record is None:
                        connection.rollback()
                        return DurableStateCorrupt()
                    snapshot = wait_answer_snapshot_from_record(stored_record)
                    if inserted.rowcount == 0:
                        if snapshot.answer.revision_hash != request.revision_hash:
                            connection.rollback()
                            return DurableAnswerRevisionConflict()
                        if snapshot.answer.answer_bytes != request.answer_bytes:
                            connection.rollback()
                            return DurableAnswerBytesConflict()
                        if snapshot.answer != answer:
                            connection.rollback()
                            return DurableStateCorrupt()
                        connection.commit()
                        return DurableAnswerExisting(snapshot)
                    try:
                        node = graph.node(request.node_id)
                    except KeyError:
                        connection.rollback()
                        return DurableAnswerNodeMissing()
                    if (
                        run.current_node_id != request.node_id
                        or run.state is not RunState.WAITING_INPUT
                        or not isinstance(node, WaitNode)
                        or node.answer_type != "integer"
                    ):
                        connection.rollback()
                        return DurableAnswerStateConflict()
                    options: EnqueueOptions = {
                        "workflow_name": ANSWER_WORKFLOW_NAME,
                        "queue_name": QUEUE_NAME,
                        "workflow_id": answer.answer_workflow_id,
                        "app_version": self._application_version,
                    }
                    client.enqueue_in_transaction(
                        connection,
                        options,
                        answer.run_id.value,
                        answer.revision_hash.value,
                        answer.node_id,
                    )
                    connection.commit()
                    return DurableAnswerCreated(snapshot)
                except (OperationalError, PoolTimeoutError):
                    connection.rollback()
                    return DurableWriteUnavailable()
                except (ValueError, RuntimeError, DatabaseError):
                    connection.rollback()
                    return DurableStateCorrupt()
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()
        finally:
            if client is not None:
                client.destroy()
