from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from dbos import DBOSClient, EnqueueOptions
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DatabaseError, OperationalError

from atelier2.adapters.dbos.effect_store import (
    intent_snapshot_from_record,
    receipt_from_record,
)
from atelier2.adapters.dbos.schema import (
    effect_intents,
    effect_receipts,
    run_events,
    runs,
    wait_answers,
    workflow_revisions,
)
from atelier2.adapters.yaml_workflows import parse_workflow_document
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
    logical_effect_key_for,
    terminal_hash_for,
)
from atelier2.contracts.hashing import Sha256Hash
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
    SubworkflowNode,
    WaitNode,
    WorkflowGraph,
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


_INTEGER_ANSWER = re.compile(rb"(?:0|-?[1-9][0-9]*)")


def load_graph(session: Any, revision_hash: WorkflowRevisionHash) -> WorkflowGraph:
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
) -> WorkflowGraph:
    revision = WorkflowRevision(bytes(document))
    if revision.revision_hash != revision_hash:
        raise RevisionHashCollision(
            "durable workflow revision bytes disagree with their hash"
        )
    return parse_workflow_document(revision.document)


def run_from_record(record: Mapping[Any, Any]) -> Run:
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


def load_run(session: Any, run_id: RunId) -> Run:
    record = (
        session.execute(sa.select(runs).where(runs.c.run_id == run_id.value))
        .mappings()
        .one_or_none()
    )
    if record is None:
        raise RunTransitionConflict("run does not exist")
    run = run_from_record(record)
    graph = load_graph(session, run.revision_hash)
    validate_run_graph_binding(run, graph)
    return run


def validate_run_graph_binding(run: Run, graph: WorkflowGraph) -> None:
    node = graph.node(run.current_node_id)
    if run.state is RunState.WAITING_INPUT and not isinstance(node, WaitNode):
        raise RunTransitionConflict("WAITING_INPUT must name a Wait node")
    if run.state is RunState.WAITING_RECONCILIATION and not isinstance(
        node, ActionNode
    ):
        raise RunTransitionConflict("WAITING_RECONCILIATION must name an Action node")
    if run.state is RunState.COMPLETED and not isinstance(node, SubworkflowNode):
        raise RunTransitionConflict("COMPLETED must name the terminal Subworkflow")


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
) -> TransitionSnapshot | None:
    record = (
        session.execute(
            sa.select(run_events).where(
                run_events.c.run_id == run_id.value,
                run_events.c.revision_hash == revision_hash.value,
                run_events.c.node_id == node_id,
                run_events.c.event_kind == event_kind.value,
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
    if terminal and not isinstance(target_node, SubworkflowNode):
        raise RunTransitionConflict("terminal transition must finish a Subworkflow")
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


def commit_agent_completed(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    output: bytes,
) -> TransitionSnapshot:
    graph = load_graph(session, revision_hash)
    return _commit_event(
        session,
        run_id,
        revision_hash,
        node_id,
        RunEventKind.AGENT_COMPLETED,
        output,
        RunState.STARTED,
        RunState.STARTED,
        graph.successor(node_id).id,
    )


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
        graph.successor(action.id).id,
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
        graph.successor(answer.node_id).id,
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

    def submit(self, request: SubmitWaitAnswerRequest) -> WaitAnswerSnapshot:
        result = self.submit_result(request)
        if isinstance(result, (DurableAnswerCreated, DurableAnswerExisting)):
            return result.snapshot
        if (
            isinstance(result, DurableAnswerStateConflict)
            and _INTEGER_ANSWER.fullmatch(request.answer_bytes) is None
        ):
            raise ValueError(
                "integer answer must be canonical base-10 bytes without whitespace or plus"
            )
        raise RunTransitionConflict(f"wait answer refused: {type(result).__name__}")

    def submit_result(self, request: SubmitWaitAnswerRequest) -> DurableAnswerResult:
        if _INTEGER_ANSWER.fullmatch(request.answer_bytes) is None:
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
        except OperationalError:
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

        client = DBOSClient(
            system_database_engine=self._engine, use_listen_notify=False
        )
        try:
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
                    run = run_from_record(run_record)
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
                except OperationalError:
                    connection.rollback()
                    return DurableWriteUnavailable()
                except (ValueError, RuntimeError, DatabaseError):
                    connection.rollback()
                    return DurableStateCorrupt()
        finally:
            client.destroy()
