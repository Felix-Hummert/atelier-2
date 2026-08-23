from __future__ import annotations

import logging
import math
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DatabaseError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from atelier2.adapters.dbos.attention_events import load_attention_event_page
from atelier2.adapters.dbos.effect_store import (
    command_snapshot_from_record,
    intent_snapshot_from_record,
    receipt_from_record,
)
from atelier2.adapters.dbos.run_store import (
    NodeOutputNotWritten,
    NodeOutputSchemaRefused,
    _agent_receipt_v2_from_record,
    load_node_outputs,
    load_run_inputs,
)
from atelier2.adapters.dbos.run_transitions import (
    RunTransitionConflict,
    event_from_record,
    run_from_record_with_bindings,
    validate_run_graph_binding,
)
from atelier2.adapters.dbos.schema import (
    agent_attempts,
    agent_receipts_v2,
    attempt_instants,
    effect_intents,
    effect_receipts,
    event_instants,
    node_receipts_v3,
    reconcile_commands,
    run_events,
    run_instants,
    runs,
    workflow_revisions,
)
from atelier2.adapters.dbos.workflow import (
    _declared_output_schema_document,
    _pinned_maximum_assistant_turns,
)
from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.application.compose_node_job import node_job
from atelier2.application.project_node_rail import (
    never_launched_cleanup_on_failed_run,
    project_node_rail,
)
from atelier2.contracts.agent_attempts import (
    AgentAttemptCancellationDisposition,
    AgentAttemptFailureCode,
    AgentAttemptId,
    AgentAttemptRedriveState,
    AgentAttemptReplacement,
    AgentAttemptState,
)
from atelier2.contracts.agents import (
    AgentExecutionRequestHash,
    AgentExecutionRequestV2,
    AgentExecutorOperationalIdentity,
)
from atelier2.contracts.effects import (
    EffectIntentState,
    ReconcileCommandId,
    ReconcileCommandState,
)
from atelier2.contracts.executions import (
    AgentExecutionRefusal,
    NodeExecutionId,
    RunEventKind,
    logical_effect_key_for,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.node_records_v3 import (
    PersistedReceiptDisposition,
    read_stored_node_receipt_reason,
)
from atelier2.contracts.pages import MAXIMUM_PAGE_ITEMS
from atelier2.contracts.run_bindings import AnyRun, RunV2, RunV3
from atelier2.contracts.run_events import (
    PersistedRunEvent,
    RunEventPage,
)
from atelier2.contracts.run_projections import (
    AgentAttemptCancellationProjection,
    AgentAttemptProjection,
    NodeAnswer,
    NodeDetail,
    NodeProvenance,
    RunPage,
    RunProjection,
    WaitingReconciliationProjection,
    public_agent_attempt_state,
)
from atelier2.contracts.runs import (
    RevisionHashCollision,
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.contracts.when import RecordedAt
from atelier2.contracts.workflow_formats import WorkflowFormatVersion
from atelier2.contracts.workflow_projections import (
    DescribedWorkflowRevisionPage,
    EnrichedPageBudget,
    WorkflowRevisionPage,
    WorkflowRevisionProjection,
)
from atelier2.contracts.workflows import AgentNodeV2, WorkflowGraphV2, round_of
from atelier2.contracts.workflows_v3 import (
    AgentNodeV3,
    AnyWorkflowDocument,
    WaitNodeV3,
    WorkflowGraphV3,
)
from atelier2.ports.run_events import (
    CursorAhead,
    EventHistoryCorrupt,
    PrepareRunEventStreamResult,
    ReadAttentionEventPageResult,
    ReadRunEventPageResult,
    StreamReady,
)
from atelier2.ports.run_queries import (
    GetNodeDetailResult,
    GetReconciliationRetryTargetResult,
    GetRunResult,
    ListRunReceiptsResult,
    ListRunsResult,
    NodeDetailFound,
    NodeQueryMissing,
    ReconciliationRetryCommandConflict,
    ReconciliationRetryTargetFound,
    ReconciliationRetryTargetMissing,
    RunFound,
    RunQueryMissing,
    RunReceiptsFound,
)
from atelier2.ports.workflow_revisions import (
    DurableProjectionLimit,
    GetWorkflowRevisionResult,
    ListDescribedWorkflowRevisionsResult,
    ListWorkflowRevisionsResult,
    ProjectionLimitExceeded,
    ProjectionTooLarge,
    QueryDurableStateCorrupt,
    ReadUnavailable,
    WorkflowRevisionFound,
    WorkflowRevisionMissing,
)

_LENGTH_LABEL_PREFIX = "_atelier_length_"
_MAXIMUM_UTF8_BYTES_PER_CHARACTER = 4
_RUN_PROJECTION_COLUMNS: tuple[sa.Column[Any], ...] = (
    runs.c.run_id,
    runs.c.revision_hash,
    runs.c.workflow_format_version,
    runs.c.agent_binding_set_hash,
    runs.c.current_node_id,
    runs.c.current_round_ordinal,
    runs.c.state,
    runs.c.state_version,
    runs.c.last_event_sequence,
    runs.c.terminal_hash,
    # A V3 run reads back as `RunV3`, which is bound to the configuration
    # revision it was started under; without this column every projection of one
    # raises rather than answering. It stayed unnoticed while no V3 run could
    # reach a public route.
    runs.c.run_configuration_revision_hash,
)
_RUN_FIELD_COLUMNS = frozenset(("run_id", "current_node_id"))
_REVISION_DOCUMENT_COLUMNS = frozenset(("document",))
_INTENT_PAYLOAD_COLUMNS = frozenset(("canonical_request",))
_INTENT_FIELD_COLUMNS = frozenset(
    (
        "logical_key",
        "run_id",
        "adapter_revision",
        "destination_identity",
        "adapter_operational_identity",
        "reconciliation_owner_command_id",
    )
)
_COMMAND_PAYLOAD_COLUMNS = frozenset(("found_result",))
_COMMAND_FIELD_COLUMNS = frozenset(
    ("command_id", "logical_key", "actor", "evidence", "found_effect_id")
)
_EVENT_PAYLOAD_COLUMNS = frozenset(("payload",))
_EVENT_FIELD_COLUMNS = frozenset(("run_id", "node_id", "receipt_logical_key"))
_ATTEMPT_FIELD_COLUMNS = frozenset(
    ("executor_operational_identity", "run_id", "node_id")
)
_RECEIPT_PAYLOAD_COLUMNS = frozenset(("canonical_request", "result"))
_RECEIPT_FIELD_COLUMNS = frozenset(
    (
        "logical_key",
        "run_id",
        "adapter_revision",
        "destination_identity",
        "adapter_operational_identity",
        "effect_id",
        "reconcile_command_id",
    )
)


def _bounded_projection_select(
    table: sa.Table,
    projection_limit: DurableProjectionLimit,
    *,
    columns: Sequence[sa.Column[Any]] | None = None,
    document_columns: frozenset[str] = frozenset(),
    payload_columns: frozenset[str] = frozenset(),
    field_columns: frozenset[str] = frozenset(),
) -> sa.Select[Any]:
    selected_columns = tuple(table.c) if columns is None else columns
    projected: list[Any] = []
    for column in selected_columns:
        if column.name in document_columns:
            maximum = projection_limit.maximum_document_bytes
            length = sa.func.length(column)
        elif column.name in payload_columns:
            maximum = projection_limit.maximum_payload_bytes
            length = sa.func.length(column)
        elif column.name in field_columns:
            maximum = (
                _MAXIMUM_UTF8_BYTES_PER_CHARACTER
                * projection_limit.maximum_field_characters
            )
            length = sa.func.length(sa.cast(column, sa.LargeBinary()))
        else:
            projected.append(column)
            continue
        projected.append(
            sa.case((length <= maximum, column), else_=None).label(column.name)
        )
        projected.append(length.label(_LENGTH_LABEL_PREFIX + column.name))
    return sa.select(*projected)


def _validate_bounded_record(
    record: Mapping[Any, Any],
    projection_limit: DurableProjectionLimit,
    *,
    document_columns: frozenset[str] = frozenset(),
    payload_columns: frozenset[str] = frozenset(),
    field_columns: frozenset[str] = frozenset(),
) -> None:
    for column_name in document_columns:
        length = record[_LENGTH_LABEL_PREFIX + column_name]
        if length is not None:
            projection_limit.validate_document_length(int(length))
    for column_name in payload_columns:
        length = record[_LENGTH_LABEL_PREFIX + column_name]
        if length is not None:
            projection_limit.validate_payload_length(int(length))
    for column_name in field_columns:
        length = record[_LENGTH_LABEL_PREFIX + column_name]
        if length is None:
            continue
        value = record[column_name]
        if value is None:
            raise ProjectionLimitExceeded(
                "durable text exceeds its response allocation limit"
            )
        projection_limit.validate_field_length(len(str(value)))


_LOG = logging.getLogger("atelier2")

_AGENT_FAILURE_FORMATS = frozenset((WorkflowFormatVersion.V2, WorkflowFormatVersion.V3))
"""Which families reach the agent attempt path, and so can record its failure."""


def _run_ending_event_predicate(
    workflow_format_version: WorkflowFormatVersion,
    current_node_execution_id: NodeExecutionId,
) -> Callable[[tuple[str, NodeExecutionId]], bool]:
    """Whether one event is the event that ended this run.

    Two families spell an ending differently, and both readers have to ask the
    question rather than name one spelling. A V1 or V2 line ends on the
    subworkflow completion of its final node, so the kind alone identifies it.

    A V3 line ends on the node it stands on -- #194 H1b lifted the terminal
    condition off the subworkflow node onto the run -- and there neither half
    identifies it alone. The kind cannot, because every agent node completes or
    fails with the same pair, a linear Action completes with its own kind, and a
    Wait node's answer completes with a third. The execution cannot either, and
    that is the less obvious half: an attempt event can advance the run's head
    without moving it. What ends a V3 run is the **completion or failure** of
    the exact execution it stands on, so both halves are asked. Exact identity
    also keeps an earlier round's completion at the same looped node from
    posing as the current round's ending.

    The four kinds below are exhaustive for every V3 node the runtime can
    currently stand a run's sink on -- Agent (two kinds), Action and Wait
    (#510). A Deterministic or Subworkflow V3 node has no execution path yet
    (`bind_node` refuses one before any event could be written for it), so
    neither belongs here until that gap is closed with its own runtime wiring.

    Asking the run row rather than parsing its document keeps this cheap: it is a
    pre-flight before stream headers and a check beside a page read, never a
    projection.
    """

    if workflow_format_version is WorkflowFormatVersion.V3:
        ending = {
            RunEventKind.AGENT_COMPLETED.value,
            RunEventKind.AGENT_FAILED.value,
            RunEventKind.ACTION_COMPLETED.value,
            RunEventKind.WAIT_ANSWERED.value,
        }
        return lambda endpoint: (
            endpoint[0] in ending and endpoint[1] == current_node_execution_id
        )
    terminal_kind = RunEventKind.SUBWORKFLOW_COMPLETED.value
    failed = RunEventKind.AGENT_FAILED.value
    return lambda endpoint: (
        endpoint[0] == terminal_kind
        or (endpoint[0] == failed and endpoint[1] == current_node_execution_id)
    )


def _node_execution_id(
    run: AnyRun, graph: AnyWorkflowDocument, node_id: str
) -> NodeExecutionId:
    return NodeExecutionId.for_node(
        run.run_id,
        run.revision_hash,
        node_id,
        round_of(graph, node_id, run.current_round_ordinal),
    )


def _event_endpoint(record: Mapping[Any, Any]) -> tuple[str, NodeExecutionId]:
    execution_id = NodeExecutionId(str(record["node_execution_id"]))
    expected = NodeExecutionId.for_node(
        RunId(str(record["run_id"])),
        WorkflowRevisionHash(str(record["revision_hash"])),
        str(record["node_id"]),
        int(record["round_ordinal"]),
    )
    if execution_id != expected:
        raise RunTransitionConflict("event node execution binding disagrees")
    return str(record["event_kind"]), execution_id


def _durable_attempt_state(persisted_value: Any) -> AgentAttemptState:
    try:
        return AgentAttemptState(str(persisted_value))
    except ValueError as outside_vocabulary:
        raise RunTransitionConflict(
            "persisted agent attempt state is outside the durable vocabulary"
        ) from outside_vocabulary


def _current_attempt_projection(
    record: Mapping[Any, Any],
    *,
    session: Connection,
    run: RunV2 | RunV3,
    graph: WorkflowGraphV2 | WorkflowGraphV3,
) -> AgentAttemptProjection:
    node = graph.node(run.current_node_id)
    if not isinstance(node, (AgentNodeV2, AgentNodeV3)):
        raise RunTransitionConflict("current attempt does not belong to an agent")
    binding = next(
        (binding for binding in run.agent_bindings if binding.role.value == node.role),
        None,
    )
    if binding is None:
        raise RunTransitionConflict("current agent has no exact durable binding")
    operational_identity = AgentExecutorOperationalIdentity(
        str(record["executor_operational_identity"])
    )
    execution_id = _node_execution_id(run, graph, run.current_node_id)
    # Recomputed through the one composition owner, with everything that owner
    # is given: the orders the run was started with and the work earlier nodes
    # handed on. A recomputation that knew only part of it would answer a run
    # that really was a chain with a conflict about its own identity.
    authored_job = (
        node.job
        if isinstance(node, AgentNodeV2)
        else node_job(
            node.instruction,
            load_run_inputs(session, run.run_id, node),
            load_node_outputs(
                session,
                run.run_id,
                run.revision_hash,
                graph,
                node,
                run.current_round_ordinal,
            ),
        )
    ).encode("utf-8")
    exact_request = AgentExecutionRequestV2(
        execution_id,
        run.run_id,
        run.revision_hash,
        run.current_node_id,
        binding,
        operational_identity,
        authored_job,
        (
            None
            if (output_schema := _declared_output_schema_document(session, node))
            is None
            else output_schema.encode("utf-8")
        ),
        run.current_round_ordinal,
        _pinned_maximum_assistant_turns(session, node),
    )
    request_hash = AgentExecutionRequestHash(str(record["request_hash"]))
    attempt_id = AgentAttemptId(str(record["attempt_id"]))
    ordinal = int(record["attempt_ordinal"])
    if (
        ordinal not in (1, 2)
        or NodeExecutionId(str(record["node_execution_id"])) != execution_id
        or RunId(str(record["run_id"])) != run.run_id
        or WorkflowRevisionHash(str(record["workflow_revision_hash"]))
        != run.revision_hash
        or str(record["node_id"]) != run.current_node_id
        or request_hash != exact_request.request_hash
        or attempt_id
        != AgentAttemptId.for_execution(
            execution_id, exact_request.request_hash, ordinal
        )
    ):
        raise RunTransitionConflict("current agent attempt binding disagrees")
    durable_state = _durable_attempt_state(record["state"])
    public_state = public_agent_attempt_state(durable_state)
    if public_state is None:
        raise RunTransitionConflict(
            "successful current attempt has no atomic successor transition"
        )
    failure_value = record["failure_code"]
    receipt_value = record["receipt_hash"]
    state_version = int(record["state_version"])
    failure: AgentAttemptFailureCode | None = None
    if durable_state is AgentAttemptState.PREPARED:
        if state_version not in (0, 1) or receipt_value is not None:
            raise RunTransitionConflict("prepared agent attempt shape disagrees")
    elif durable_state is AgentAttemptState.LAUNCH_ARMED:
        if state_version < 1 or receipt_value is not None:
            raise RunTransitionConflict("armed agent attempt shape disagrees")
    elif durable_state is AgentAttemptState.FAILED:
        if state_version < 2 or receipt_value is not None:
            raise RunTransitionConflict("failed agent attempt shape disagrees")
        failure = AgentAttemptFailureCode(str(failure_value))
    elif durable_state in {
        AgentAttemptState.CANCEL_REQUESTED,
        AgentAttemptState.CANCELLED,
        AgentAttemptState.INTERRUPTED,
    }:
        if receipt_value is not None or record["cancellation_command_id"] is None:
            raise RunTransitionConflict("cancelled agent attempt shape disagrees")
    else:
        raise RunTransitionConflict("agent attempt state has no projected shape")
    if (failure_value is None) != (failure is None):
        raise RunTransitionConflict("current agent attempt failure shape disagrees")
    command_id = record["cancellation_command_id"]
    disposition = record["cancellation_disposition"]
    cancellation = (
        None
        if command_id is None
        else AgentAttemptCancellationProjection(
            str(command_id),
            AgentAttemptReplacement(str(record["replacement"])),
            AgentAttemptRedriveState(str(record["redrive_state"])),
            (
                None
                if disposition is None
                else AgentAttemptCancellationDisposition(str(disposition))
            ),
        )
    )
    return AgentAttemptProjection(
        attempt_id,
        execution_id,
        request_hash,
        ordinal,
        public_state,
        failure,
        cancellation,
    )


def _node_receipt_refusal(
    connection: Connection,
    execution_id: NodeExecutionId,
) -> str | None:
    """The durably named reason this node's execution ended without a success.

    Read from `node-receipt/v3` where one exists: the receipt was written in
    the transaction that ended the execution, so it is the store's own
    statement and outranks a recomputation. A succeeded receipt refuses
    nothing, and a run from before the family's writer has no receipt at all --
    that absence is honest, never filled in here.
    """
    record = connection.execute(
        sa.select(node_receipts_v3.c.disposition, node_receipts_v3.c.reason).where(
            node_receipts_v3.c.node_execution_id == execution_id.value
        )
    ).one_or_none()
    if record is None:
        return None
    disposition = PersistedReceiptDisposition(str(record.disposition))
    if disposition is PersistedReceiptDisposition.SUCCEEDED:
        return None
    reason, _schema_revision, _value_hash = read_stored_node_receipt_reason(
        str(record.reason)
    )
    return reason


def _unavailable_executor_refusal(
    connection: Connection,
    execution_id: NodeExecutionId,
) -> str | None:
    """Read the terminal pre-attempt refusal written for a declared binding.

    This durable terminal event deliberately has no node receipt or attempt:
    the executor was known to be unavailable before a provider invocation could
    begin. Its product reason is nevertheless part of the run's own record,
    rather than a fresh current-host recomputation.
    """
    event = connection.execute(
        sa.select(run_events.c.payload).where(
            run_events.c.node_execution_id == execution_id.value,
            run_events.c.event_kind == RunEventKind.AGENT_FAILED.value,
            run_events.c.agent_attempt_id.is_(None),
            run_events.c.attempt_ordinal.is_(None),
        )
    ).one_or_none()
    if event is None:
        return None
    if event.payload != AgentExecutionRefusal.EXECUTOR_BINDING_UNAVAILABLE.value.encode(
        "ascii"
    ):
        return None
    return AgentExecutionRefusal.EXECUTOR_BINDING_UNAVAILABLE.value


def _node_job_and_refusal(
    connection: Connection,
    projection: RunProjection,
    node: object,
    round_ordinal: int,
) -> tuple[bytes | None, str | None, str | None]:
    """What this node was handed, and what stops it if something does.

    A V1 or V2 node's job is the text its author wrote and nothing else, so there
    is nothing to refuse. A V3 agent node is composed from its instruction, the
    orders the run carries and the work earlier nodes handed on -- and composing
    it is exactly where a refusal surfaces, because reading an earlier node's
    value against the schema its author pinned happens there. The composer's
    refusal is caught rather than raised: an operator asking about a stuck node
    wants to be told the reason, not to be refused the question.
    """

    if isinstance(node, AgentNodeV2):
        job = node.job.encode("utf-8")
        return job, Sha256Hash.of(job).value, None
    if isinstance(node, WaitNodeV3):
        # The published document already names what the person is asked. This
        # read uses that parse, not the executable one, so a wait that holds a
        # run still answers with its authored question.
        job = node.prompt.encode("utf-8")
        return job, Sha256Hash.of(job).value, None
    if not isinstance(node, AgentNodeV3):
        return None, None, None
    run = projection.run
    try:
        composed = node_job(
            node.instruction,
            load_run_inputs(connection, run.run_id, node),
            load_node_outputs(
                connection,
                run.run_id,
                run.revision_hash,
                projection.graph,
                node,
                round_ordinal,
            ),
        ).encode("utf-8")
    except NodeOutputNotWritten:
        # Absence, not refusal. The node this one reads has not written yet, so
        # there is no job to prove and nothing has judged anything. Saying so as
        # a refusal would report a waiting run as a stopped one.
        return None, None, None
    except NodeOutputSchemaRefused as refused:
        return None, None, str(refused)
    return composed, Sha256Hash.of(composed).value, None


def _node_instants(
    connection: Connection, execution_id: NodeExecutionId
) -> tuple[RecordedAt | None, RecordedAt | None]:
    """The first start and last end recorded for this node's attempts.

    An Agent node's attempts carry that window in their own instants table. A
    Wait node has no attempt row at all -- nothing runs between the run
    reaching it and a person answering it -- so an empty attempt result falls
    through to the single instant its answer was recorded at.
    """

    rows = tuple(
        connection.execute(
            sa.select(attempt_instants.c.started_at, attempt_instants.c.ended_at)
            .select_from(
                attempt_instants.join(
                    agent_attempts,
                    attempt_instants.c.attempt_id == agent_attempts.c.attempt_id,
                )
            )
            .where(agent_attempts.c.node_execution_id == execution_id.value)
            .order_by(agent_attempts.c.attempt_ordinal)
        ).mappings()
    )
    if not rows:
        return _node_wait_answered_instant(connection, execution_id)
    started = RecordedAt(str(rows[0]["started_at"]))
    ended_values = [record["ended_at"] for record in rows]
    if any(value is None for value in ended_values):
        return started, None
    return started, RecordedAt(str(max(str(value) for value in ended_values)))


def _node_wait_answered_instant(
    connection: Connection, execution_id: NodeExecutionId
) -> tuple[RecordedAt | None, RecordedAt | None]:
    """A Wait node's window: the one instant its answer was recorded at.

    Unlike an Agent attempt, a Wait node has no separate started/ended pair to
    read -- the person's answer is the only thing that happened, so it stands
    for both ends of the window. `event_instants` exists from V22 on; a run
    answered before that build wrote no such row, and the honest read for it is
    nothing, not a guess.
    """

    recorded_at = connection.execute(
        sa.select(event_instants.c.recorded_at)
        .select_from(
            run_events.join(
                event_instants,
                sa.and_(
                    event_instants.c.run_id == run_events.c.run_id,
                    event_instants.c.event_sequence == run_events.c.event_sequence,
                ),
            )
        )
        .where(
            run_events.c.node_execution_id == execution_id.value,
            run_events.c.event_kind == RunEventKind.WAIT_ANSWERED.value,
        )
    ).scalar()
    if recorded_at is None:
        return None, None
    instant = RecordedAt(str(recorded_at))
    return instant, instant


ANSWER_BEARING_EVENT_KINDS: frozenset[str] = frozenset(
    kind.value
    for kind in (
        RunEventKind.AGENT_COMPLETED,
        RunEventKind.WAIT_ANSWERED,
        RunEventKind.ACTION_COMPLETED,
        RunEventKind.SUBWORKFLOW_COMPLETED,
    )
)
"""Every event kind whose payload is a node's produced value.

This is its own set rather than a reuse of `_run_ending_event_predicate`'s V3
ending kinds: that one names what closes a V3 run's current execution, scoped
to the format that can stand a run's sink on a bare Action or Wait node. This
one names what a value-bearing write looks like at all, read for V1, V2 and V3
alike wherever a node's own answer is asked for -- the two sets share members
by coincidence of what "finished" means, not by one owning the other's rule.
"""


def _node_answer(
    connection: Connection,
    execution_id: NodeExecutionId,
) -> NodeAnswer | None:
    """The value this node wrote, or nothing when it has written none yet."""

    record = connection.execute(
        sa.select(run_events.c.payload, run_events.c.payload_hash).where(
            run_events.c.node_execution_id == execution_id.value,
            run_events.c.event_kind.in_(ANSWER_BEARING_EVENT_KINDS),
        )
    ).one_or_none()
    if record is None:
        return None
    return NodeAnswer(bytes(record.payload), Sha256Hash(str(record.payload_hash)))


def _node_provenance(
    connection: Connection, execution_id: NodeExecutionId
) -> NodeProvenance | None:
    """Which agent produced this node's answer, as its receipt recorded it."""

    record = (
        connection.execute(
            sa.select(agent_receipts_v2).where(
                agent_receipts_v2.c.node_execution_id == execution_id.value,
            )
        )
        .mappings()
        .one_or_none()
    )
    if record is None:
        return None
    return NodeProvenance(
        role=str(record["role"]),
        provider_id=str(record["provider_id"]),
        model=str(record["model"]),
        executor_revision=str(record["executor_revision"]),
        executor_operational_identity=str(record["executor_operational_identity"]),
        auth_mode=str(record["auth_mode"]),
        profile_id=str(record["profile_id"]),
        agent_configuration_revision_hash=str(
            record["agent_configuration_revision_hash"]
        ),
        request_hash=str(record["request_hash"]),
        receipt_hash=str(record["receipt_hash"]),
    )


class DbosQueries:
    """Bounded SQLite projections; each call owns and closes its read connection."""

    def __init__(
        self,
        engine: Engine,
        projection_limit: DurableProjectionLimit,
        *,
        busy_timeout_seconds: float = 5.0,
        query_deadline_seconds: float = 5.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            not math.isfinite(busy_timeout_seconds)
            or not math.isfinite(query_deadline_seconds)
            or busy_timeout_seconds < 0.001
            or query_deadline_seconds <= 0
        ):
            raise ValueError(
                "query deadline must be finite and positive and SQLite busy timeout "
                "must be finite and at least one millisecond"
            )
        self._engine = engine
        self._projection_limit = projection_limit
        self._busy_timeout_milliseconds = int(busy_timeout_seconds * 1000)
        self._query_deadline_seconds = query_deadline_seconds
        self._monotonic = monotonic

    @contextmanager
    def _connection(self) -> Iterator[Connection]:
        with self._engine.connect() as connection:
            raw = connection.connection.driver_connection
            if not isinstance(raw, sqlite3.Connection):
                raise TypeError("durable query adapter requires SQLite")
            original_busy_timeout = int(
                connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
            )
            try:
                connection.exec_driver_sql(
                    f"PRAGMA busy_timeout={self._busy_timeout_milliseconds}"
                )
                deadline = self._monotonic() + self._query_deadline_seconds
                raw.set_progress_handler(
                    lambda: int(self._monotonic() >= deadline), 1000
                )
                connection.exec_driver_sql("BEGIN DEFERRED")
                yield connection
            finally:
                try:
                    raw.set_progress_handler(None, 0)
                finally:
                    try:
                        connection.rollback()
                    finally:
                        connection.exec_driver_sql(
                            f"PRAGMA busy_timeout={original_busy_timeout}"
                        )

    def get_workflow_revision(
        self,
        revision_hash: WorkflowRevisionHash,
    ) -> GetWorkflowRevisionResult:
        try:
            with self._connection() as connection:
                record = (
                    connection.execute(
                        _bounded_projection_select(
                            workflow_revisions,
                            self._projection_limit,
                            document_columns=_REVISION_DOCUMENT_COLUMNS,
                        ).where(
                            workflow_revisions.c.revision_hash == revision_hash.value
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if record is None:
                    return WorkflowRevisionMissing()
                _validate_bounded_record(
                    record,
                    self._projection_limit,
                    document_columns=_REVISION_DOCUMENT_COLUMNS,
                )
                document_bytes = bytes(record["document"])
                self._projection_limit.validate_document(document_bytes)
                revision = WorkflowRevision(document_bytes)
                if revision.revision_hash != revision_hash:
                    return QueryDurableStateCorrupt()
                graph = parse_workflow_document(revision.document)
                self._projection_limit.validate_graph(graph)
                return WorkflowRevisionFound(
                    WorkflowRevisionProjection(revision, graph)
                )
        except ProjectionLimitExceeded:
            return ProjectionTooLarge()
        except (OperationalError, PoolTimeoutError):
            return ReadUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return QueryDurableStateCorrupt()

    def list_workflow_revisions(
        self, after: WorkflowRevisionHash | None, limit: int
    ) -> ListWorkflowRevisionsResult:
        if type(limit) is not int or not 1 <= limit <= MAXIMUM_PAGE_ITEMS:
            raise ValueError(
                f"revision page limit must be an integer from 1 to {MAXIMUM_PAGE_ITEMS}"
            )
        try:
            with self._connection() as connection:
                statement = sa.select(workflow_revisions.c.revision_hash)
                if after is not None:
                    statement = statement.where(
                        workflow_revisions.c.revision_hash > after.value
                    )
                values = tuple(
                    WorkflowRevisionHash(str(value))
                    for value in connection.execute(
                        statement.order_by(workflow_revisions.c.revision_hash).limit(
                            limit + 1
                        )
                    ).scalars()
                )
                has_more = len(values) > limit
                items = values[:limit]
                return WorkflowRevisionPage(
                    items, items[-1] if has_more and items else None
                )
        except (OperationalError, PoolTimeoutError):
            return ReadUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return QueryDurableStateCorrupt()

    def list_described_workflow_revisions(
        self,
        after: WorkflowRevisionHash | None,
        limit: int,
        budget: EnrichedPageBudget,
    ) -> ListDescribedWorkflowRevisionsResult:
        """One page of revisions with their documents, in one bounded query.

        The rows stream one at a time on purpose: a page that fetched its whole
        limit before spending its budget would move every document it then
        refused to use, which is the byte cost the budget exists to bound.
        """

        if type(limit) is not int or not 1 <= limit <= MAXIMUM_PAGE_ITEMS:
            raise ValueError(
                f"revision page limit must be an integer from 1 to {MAXIMUM_PAGE_ITEMS}"
            )
        try:
            with self._connection() as connection:
                statement = sa.select(
                    workflow_revisions.c.revision_hash, workflow_revisions.c.document
                )
                if after is not None:
                    statement = statement.where(
                        workflow_revisions.c.revision_hash > after.value
                    )
                streamed = connection.execution_options(yield_per=1).execute(
                    statement.order_by(workflow_revisions.c.revision_hash).limit(
                        limit + 1
                    )
                )
                items: list[WorkflowRevisionProjection] = []
                spent_nodes = 0
                spent_bytes = 0
                exhausted = False
                for record in streamed.mappings():
                    if len(items) == limit:
                        exhausted = True
                        break
                    document = bytes(record["document"])
                    if items and spent_bytes + len(document) > (
                        budget.maximum_document_bytes
                    ):
                        exhausted = True
                        break
                    self._projection_limit.validate_document(document)
                    revision = WorkflowRevision(document)
                    if revision.revision_hash.value != str(record["revision_hash"]):
                        return QueryDurableStateCorrupt()
                    graph = parse_workflow_document(document)
                    self._projection_limit.validate_graph(graph)
                    if items and spent_nodes + len(graph.nodes) > budget.maximum_nodes:
                        exhausted = True
                        break
                    items.append(WorkflowRevisionProjection(revision, graph))
                    spent_bytes += len(document)
                    spent_nodes += len(graph.nodes)
                streamed.close()
                return DescribedWorkflowRevisionPage(
                    tuple(items),
                    items[-1].revision.revision_hash if exhausted and items else None,
                )
        except ProjectionLimitExceeded:
            return ProjectionTooLarge()
        except (OperationalError, PoolTimeoutError):
            return ReadUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return QueryDurableStateCorrupt()

    def get_node_detail(self, run_id: RunId, node_id: str) -> GetNodeDetailResult:
        """One node of one run, answered from what the run really kept.

        Three of the four answers are read; the fourth is recomputed. The job is
        not stored anywhere, so it is composed again through the one owner that
        composed it for the provider. Its plain byte hash travels as job_hash;
        the hash a reader holds against the receipt is provenance.request_hash,
        which frames execution identity, revision, binding and operational
        identity around those bytes. Doing it any other way would mean keeping
        a second copy of a value that already has an identity.

        A refusal has two voices and the durable one wins. When a node's own
        output does not satisfy the schema its author pinned, the run stops
        there and the terminal write names the reason in a `failed`
        `node-receipt/v3`; that stored statement is read back here. A run from
        before the record family's writer has no receipt and stays honestly
        absent in those tables, so its refusal is still recomputed through the
        composition owner -- named either way, so the operator is told why a run
        stands still instead of watching it stand still.
        """

        found = self.get_run(run_id)
        if not isinstance(found, RunFound):
            return found
        projection = found.projection
        try:
            with self._connection() as connection:
                try:
                    node = projection.graph.node(node_id)
                except KeyError:
                    return NodeQueryMissing()
                rail = {
                    entry.node_id: entry.state
                    for entry in project_node_rail(projection, ())
                }
                if node_id not in rail:
                    return NodeQueryMissing()
                round_ordinal = round_of(
                    projection.graph,
                    node_id,
                    projection.run.current_round_ordinal,
                )
                execution_id = _node_execution_id(
                    projection.run, projection.graph, node_id
                )
                job, job_hash, refusal = _node_job_and_refusal(
                    connection, projection, node, round_ordinal
                )
                durable_refusal = _node_receipt_refusal(
                    connection, execution_id
                ) or _unavailable_executor_refusal(connection, execution_id)
                started_at, ended_at = _node_instants(connection, execution_id)
                return NodeDetailFound(
                    NodeDetail(
                        run_id=run_id,
                        node_id=node_id,
                        state=rail[node_id],
                        job=job,
                        job_hash=job_hash,
                        answer=_node_answer(connection, execution_id),
                        provenance=_node_provenance(connection, execution_id),
                        refusal=durable_refusal
                        if durable_refusal is not None
                        else refusal,
                        started_at=started_at,
                        ended_at=ended_at,
                    )
                )
        except ProjectionLimitExceeded:
            return ProjectionTooLarge()
        except (OperationalError, PoolTimeoutError):
            return ReadUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return QueryDurableStateCorrupt()

    def list_run_receipts(self, run_id: RunId) -> ListRunReceiptsResult:
        """The agent receipts this run has written, or why they cannot be read."""
        try:
            with self._connection() as connection:
                present = connection.execute(
                    sa.select(runs.c.run_id).where(runs.c.run_id == run_id.value)
                ).first()
                if present is None:
                    return RunQueryMissing()
                records = tuple(
                    connection.execute(
                        sa.select(agent_receipts_v2)
                        .where(agent_receipts_v2.c.run_id == run_id.value)
                        .order_by(agent_receipts_v2.c.node_id)
                    ).mappings()
                )
                return RunReceiptsFound(
                    tuple(_agent_receipt_v2_from_record(record) for record in records)
                )
        except (OperationalError, PoolTimeoutError):
            return ReadUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return QueryDurableStateCorrupt()

    def get_run(
        self,
        run_id: RunId,
    ) -> GetRunResult:
        try:
            with self._connection() as connection:
                record = (
                    connection.execute(
                        _bounded_projection_select(
                            runs,
                            self._projection_limit,
                            columns=_RUN_PROJECTION_COLUMNS,
                            field_columns=_RUN_FIELD_COLUMNS,
                        ).where(runs.c.run_id == run_id.value)
                    )
                    .mappings()
                    .one_or_none()
                )
                if record is None:
                    return RunQueryMissing()
                _validate_bounded_record(
                    record,
                    self._projection_limit,
                    field_columns=_RUN_FIELD_COLUMNS,
                )
                return RunFound(self._run_projections(connection, (record,))[0])
        except ProjectionLimitExceeded:
            return ProjectionTooLarge()
        except (OperationalError, PoolTimeoutError):
            return ReadUnavailable()
        except (ValueError, RuntimeError, DatabaseError) as error:
            _LOG.error(
                "run get projection failed",
                exc_info=error,
                extra={"event": "run_get_projection_corrupt"},
            )
            return QueryDurableStateCorrupt()

    def list_runs(
        self,
        after: RunId | None,
        limit: int,
        state: RunState | None = None,
    ) -> ListRunsResult:
        if type(limit) is not int or not 1 <= limit <= MAXIMUM_PAGE_ITEMS:
            raise ValueError(
                f"run page limit must be an integer from 1 to {MAXIMUM_PAGE_ITEMS}"
            )
        try:
            with self._connection() as connection:
                statement = _bounded_projection_select(
                    runs,
                    self._projection_limit,
                    columns=_RUN_PROJECTION_COLUMNS,
                    field_columns=_RUN_FIELD_COLUMNS,
                )
                if after is not None:
                    statement = statement.where(runs.c.run_id > after.value)
                if state is not None:
                    statement = statement.where(runs.c.state == state.value)
                records = tuple(
                    connection.execute(
                        statement.order_by(runs.c.run_id).limit(limit + 1)
                    ).mappings()
                )
                has_more = len(records) > limit
                item_records = records[:limit]
                for record in item_records:
                    _validate_bounded_record(
                        record,
                        self._projection_limit,
                        field_columns=_RUN_FIELD_COLUMNS,
                    )
                projections = self._run_projections(connection, item_records)
                ordered_bytes = tuple(
                    projection.run.run_id.value.encode("utf-8")
                    for projection in projections
                )
                if ordered_bytes != tuple(sorted(ordered_bytes)) or (
                    after is not None
                    and ordered_bytes
                    and ordered_bytes[0] <= after.value.encode("utf-8")
                ):
                    raise RunTransitionConflict(
                        "SQLite run order disagrees with exact UTF-8 byte order"
                    )
                return RunPage(
                    projections,
                    (projections[-1].run.run_id if has_more and projections else None),
                )
        except ProjectionLimitExceeded:
            return ProjectionTooLarge()
        except (OperationalError, PoolTimeoutError):
            return ReadUnavailable()
        except (UnicodeEncodeError, ValueError, RuntimeError, DatabaseError) as error:
            _LOG.error(
                "run list projection failed",
                exc_info=error,
                extra={"event": "run_list_projection_corrupt"},
            )
            return QueryDurableStateCorrupt()

    def get_reconciliation_retry_target(
        self,
        run_id: RunId,
        command_id: ReconcileCommandId,
    ) -> GetReconciliationRetryTargetResult:
        try:
            with self._connection() as connection:
                run_exists = connection.scalar(
                    sa.select(sa.literal(True)).where(runs.c.run_id == run_id.value)
                )
                if run_exists is None:
                    return RunQueryMissing()
                command_record = (
                    connection.execute(
                        _bounded_projection_select(
                            reconcile_commands,
                            self._projection_limit,
                            payload_columns=_COMMAND_PAYLOAD_COLUMNS,
                            field_columns=_COMMAND_FIELD_COLUMNS,
                        ).where(reconcile_commands.c.command_id == command_id.value)
                    )
                    .mappings()
                    .one_or_none()
                )
                if command_record is None:
                    return ReconciliationRetryTargetMissing()
                _validate_bounded_record(
                    command_record,
                    self._projection_limit,
                    payload_columns=_COMMAND_PAYLOAD_COLUMNS,
                    field_columns=_COMMAND_FIELD_COLUMNS,
                )
                intent_record = (
                    connection.execute(
                        _bounded_projection_select(
                            effect_intents,
                            self._projection_limit,
                            payload_columns=_INTENT_PAYLOAD_COLUMNS,
                            field_columns=_INTENT_FIELD_COLUMNS,
                        ).where(
                            effect_intents.c.logical_key
                            == command_record["logical_key"]
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if intent_record is None:
                    return QueryDurableStateCorrupt()
                _validate_bounded_record(
                    intent_record,
                    self._projection_limit,
                    payload_columns=_INTENT_PAYLOAD_COLUMNS,
                    field_columns=_INTENT_FIELD_COLUMNS,
                )
                intent = intent_snapshot_from_record(intent_record)
                if intent.intent.binding.run_id != run_id:
                    return ReconciliationRetryCommandConflict()
                return ReconciliationRetryTargetFound(intent)
        except ProjectionLimitExceeded:
            return ProjectionTooLarge()
        except (OperationalError, PoolTimeoutError):
            return ReadUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return QueryDurableStateCorrupt()

    def _run_projections(
        self,
        connection: Connection,
        records: Sequence[Mapping[Any, Any]],
    ) -> tuple[RunProjection, ...]:
        if not records:
            return ()
        loaded_runs = tuple(
            run_from_record_with_bindings(connection, record) for record in records
        )
        revision_hashes = {run.revision_hash for run in loaded_runs}
        revision_rows = tuple(
            connection.execute(
                _bounded_projection_select(
                    workflow_revisions,
                    self._projection_limit,
                    document_columns=_REVISION_DOCUMENT_COLUMNS,
                ).where(
                    workflow_revisions.c.revision_hash.in_(
                        tuple(value.value for value in revision_hashes)
                    )
                )
            ).mappings()
        )
        for record in revision_rows:
            _validate_bounded_record(
                record,
                self._projection_limit,
                document_columns=_REVISION_DOCUMENT_COLUMNS,
            )
        revision_records = {
            WorkflowRevisionHash(str(record["revision_hash"])): bytes(
                record["document"]
            )
            for record in revision_rows
        }
        if set(revision_records) != revision_hashes:
            raise RunTransitionConflict(
                "run page references a missing workflow revision"
            )
        graphs = {}
        for revision_hash, document in revision_records.items():
            self._projection_limit.validate_document(document)
            stored = WorkflowRevision(document)
            if stored.revision_hash != revision_hash:
                raise RevisionHashCollision(
                    "durable workflow revision bytes disagree with their hash"
                )
            # A run already started against these bytes. Today's executable
            # parse may refuse the same document; listing and inspecting it
            # is a read of published history, not a start.
            graph = parse_workflow_document(document)
            self._projection_limit.validate_graph(graph)
            graphs[revision_hash] = graph
        for run in loaded_runs:
            validate_run_graph_binding(run, graphs[run.revision_hash])

        current_agent_executions = {
            run.run_id: _node_execution_id(
                run, graphs[run.revision_hash], run.current_node_id
            )
            for run in loaded_runs
            if isinstance(
                graphs[run.revision_hash].node(run.current_node_id),
                (AgentNodeV2, AgentNodeV3),
            )
        }
        attempt_records: dict[str, list[Mapping[Any, Any]]] = {}
        if current_agent_executions:
            for record in connection.execute(
                _bounded_projection_select(
                    agent_attempts,
                    self._projection_limit,
                    field_columns=_ATTEMPT_FIELD_COLUMNS,
                ).where(
                    agent_attempts.c.node_execution_id.in_(
                        tuple(
                            execution.value
                            for execution in current_agent_executions.values()
                        )
                    )
                )
            ).mappings():
                _validate_bounded_record(
                    record,
                    self._projection_limit,
                    field_columns=_ATTEMPT_FIELD_COLUMNS,
                )
                execution_value = str(record["node_execution_id"])
                attempt_records.setdefault(execution_value, []).append(record)
            for records_for_execution in attempt_records.values():
                records_for_execution.sort(
                    key=lambda item: int(item["attempt_ordinal"])
                )
                ordinals = tuple(
                    int(item["attempt_ordinal"]) for item in records_for_execution
                )
                if ordinals not in {(1,), (1, 2)}:
                    raise RunTransitionConflict(
                        "current node has a noncanonical agent-attempt sequence"
                    )

        waiting_runs = tuple(
            run for run in loaded_runs if run.state is RunState.WAITING_RECONCILIATION
        )
        logical_keys_by_run = {
            run.run_id: logical_effect_key_for(
                _node_execution_id(run, graphs[run.revision_hash], run.current_node_id)
            )
            for run in waiting_runs
        }
        intent_records: dict[str, Mapping[Any, Any]] = {}
        if waiting_runs:
            for record in connection.execute(
                _bounded_projection_select(
                    effect_intents,
                    self._projection_limit,
                    payload_columns=_INTENT_PAYLOAD_COLUMNS,
                    field_columns=_INTENT_FIELD_COLUMNS,
                ).where(
                    effect_intents.c.logical_key.in_(
                        tuple(key.value for key in logical_keys_by_run.values())
                    )
                )
            ).mappings():
                _validate_bounded_record(
                    record,
                    self._projection_limit,
                    payload_columns=_INTENT_PAYLOAD_COLUMNS,
                    field_columns=_INTENT_FIELD_COLUMNS,
                )
                key = str(record["logical_key"])
                if key in intent_records:
                    raise RunTransitionConflict("durable intent primary key repeated")
                intent_records[key] = record
        if set(intent_records) != {key.value for key in logical_keys_by_run.values()}:
            raise RunTransitionConflict(
                "WAITING_RECONCILIATION run has no exact durable intent"
            )

        owner_ids = tuple(
            str(record["reconciliation_owner_command_id"])
            for record in intent_records.values()
            if record["reconciliation_owner_command_id"] is not None
        )
        command_rows = (
            tuple(
                connection.execute(
                    _bounded_projection_select(
                        reconcile_commands,
                        self._projection_limit,
                        payload_columns=_COMMAND_PAYLOAD_COLUMNS,
                        field_columns=_COMMAND_FIELD_COLUMNS,
                    ).where(reconcile_commands.c.command_id.in_(owner_ids))
                ).mappings()
            )
            if owner_ids
            else ()
        )
        for record in command_rows:
            _validate_bounded_record(
                record,
                self._projection_limit,
                payload_columns=_COMMAND_PAYLOAD_COLUMNS,
                field_columns=_COMMAND_FIELD_COLUMNS,
            )
        command_records = {str(record["command_id"]): record for record in command_rows}
        if set(command_records) != set(owner_ids):
            raise RunTransitionConflict("reconciling intent command is missing")

        instants: dict[str, tuple[RecordedAt, RecordedAt | None]] = {}
        run_ids = tuple(run.run_id.value for run in loaded_runs)
        instant_rows = (
            connection.execute(
                sa.select(run_instants).where(run_instants.c.run_id.in_(run_ids))
            ).mappings()
            if run_ids
            else ()
        )
        for record in instant_rows:
            ended = record["ended_at"]
            instants[str(record["run_id"])] = (
                RecordedAt(str(record["started_at"])),
                None if ended is None else RecordedAt(str(ended)),
            )

        projections = []
        for run in loaded_runs:
            reconciliation: WaitingReconciliationProjection | None = None
            if run.state is RunState.WAITING_RECONCILIATION:
                logical_key = logical_keys_by_run[run.run_id]
                intent_record = intent_records[logical_key.value]
                intent = intent_snapshot_from_record(intent_record)
                if (
                    intent.intent.binding.run_id != run.run_id
                    or intent.intent.binding.workflow_revision_hash != run.revision_hash
                    or intent.intent.binding.logical_key != logical_key
                ):
                    raise RunTransitionConflict(
                        "waiting run intent binding disagrees with its logical key"
                    )
                pending = None
                owner = intent_record["reconciliation_owner_command_id"]
                if intent.state is EffectIntentState.RECONCILING:
                    if owner is None:
                        raise RunTransitionConflict(
                            "reconciling intent has no command owner"
                        )
                    pending = command_snapshot_from_record(
                        command_records[str(owner)], intent.intent
                    )
                    if pending.state is not ReconcileCommandState.PENDING:
                        raise RunTransitionConflict(
                            "reconciling intent command is not pending"
                        )
                elif (
                    intent.state is not EffectIntentState.WAITING_RECONCILIATION
                    or owner is not None
                ):
                    raise RunTransitionConflict(
                        "waiting reconciliation run has inconsistent intent state"
                    )
                reconciliation = WaitingReconciliationProjection(intent, pending)
            attempt_projections: tuple[AgentAttemptProjection, ...] = ()
            execution = current_agent_executions.get(run.run_id)
            if execution is not None:
                if not isinstance(run, (RunV2, RunV3)):
                    raise RunTransitionConflict("agent node belongs to a V1 run")
                records_for_execution = attempt_records.get(execution.value, [])
                # A succeeded attempt has no public state, so projecting it
                # would refuse the read. COMPLETED is that case. FAILED is
                # not: the attempt is still the current one, and the rail
                # needs it so a list read does not pose the node as working.
                # NEVER_LAUNCHED cleanup on a FAILED run is the exception: it
                # is control evidence for an attempt-less refusal, not the
                # public node ending.
                if records_for_execution and run.state is not RunState.COMPLETED:
                    graph = graphs[run.revision_hash]
                    if not isinstance(graph, (WorkflowGraphV2, WorkflowGraphV3)):
                        raise RunTransitionConflict("bound run has a V1 workflow graph")
                    attempt_projections = tuple(
                        _current_attempt_projection(
                            attempt_record,
                            session=connection,
                            run=run,
                            graph=graph,
                        )
                        for attempt_record in records_for_execution
                    )
                    attempt_projections = tuple(
                        attempt
                        for attempt in attempt_projections
                        if not never_launched_cleanup_on_failed_run(run, attempt)
                    )
            instant = instants.get(run.run_id.value)
            projections.append(
                RunProjection(
                    run,
                    graphs[run.revision_hash],
                    reconciliation,
                    attempt_projections,
                    None if instant is None else instant[0],
                    None if instant is None else instant[1],
                )
            )
        return tuple(projections)

    def prepare_run_event_stream(
        self, run_id: RunId, after_sequence: int
    ) -> PrepareRunEventStreamResult:
        if type(after_sequence) is not int or after_sequence < 0:
            return EventHistoryCorrupt()
        try:
            with self._connection() as connection:
                record = (
                    connection.execute(
                        sa.select(
                            runs.c.state,
                            runs.c.last_event_sequence,
                            runs.c.workflow_format_version,
                            runs.c.revision_hash,
                            runs.c.current_node_id,
                            runs.c.current_round_ordinal,
                        ).where(runs.c.run_id == run_id.value)
                    )
                    .mappings()
                    .one_or_none()
                )
                if record is None:
                    return RunQueryMissing()
                head = int(record["last_event_sequence"])
                if after_sequence > head:
                    return CursorAhead()
                terminal = str(record["state"]) in {
                    RunState.COMPLETED.value,
                    RunState.FAILED.value,
                }
                if head == 0:
                    first_sequence = connection.scalar(
                        sa.select(run_events.c.event_sequence)
                        .where(run_events.c.run_id == run_id.value)
                        .order_by(run_events.c.event_sequence)
                        .limit(1)
                    )
                    if first_sequence is not None or terminal:
                        return EventHistoryCorrupt()
                    return StreamReady(head, terminal, after_sequence)

                required_sequences = {1, head}
                if after_sequence > 0:
                    required_sequences.add(after_sequence)
                endpoint_records = {
                    int(endpoint["event_sequence"]): _event_endpoint(endpoint)
                    for endpoint in connection.execute(
                        sa.select(
                            run_events.c.event_sequence,
                            run_events.c.event_kind,
                            run_events.c.run_id,
                            run_events.c.revision_hash,
                            run_events.c.node_id,
                            run_events.c.node_execution_id,
                            run_events.c.round_ordinal,
                        ).where(
                            run_events.c.run_id == run_id.value,
                            run_events.c.event_sequence.in_(required_sequences),
                        )
                    ).mappings()
                }
                if set(endpoint_records) != required_sequences:
                    return EventHistoryCorrupt()
                ended_the_run = _run_ending_event_predicate(
                    WorkflowFormatVersion(int(record["workflow_format_version"])),
                    NodeExecutionId.for_node(
                        run_id,
                        WorkflowRevisionHash(str(record["revision_hash"])),
                        str(record["current_node_id"]),
                        int(record["current_round_ordinal"]),
                    ),
                )
                if ended_the_run(endpoint_records[head]) != terminal or any(
                    sequence < head and ended_the_run(endpoint)
                    for sequence, endpoint in endpoint_records.items()
                ):
                    return EventHistoryCorrupt()
                return StreamReady(head, terminal, after_sequence)
        except (OperationalError, PoolTimeoutError):
            return ReadUnavailable()
        except (TypeError, ValueError, RuntimeError, DatabaseError):
            return QueryDurableStateCorrupt()

    def read_run_event_page(
        self,
        run_id: RunId,
        after_sequence: int,
        limit: int,
    ) -> ReadRunEventPageResult:
        if type(limit) is not int or not 1 <= limit <= MAXIMUM_PAGE_ITEMS:
            raise ValueError(
                f"event page limit must be an integer from 1 to {MAXIMUM_PAGE_ITEMS}"
            )
        try:
            with self._connection() as connection:
                run_record = (
                    connection.execute(
                        sa.select(
                            runs.c.state,
                            runs.c.last_event_sequence,
                            runs.c.workflow_format_version,
                            runs.c.revision_hash,
                            runs.c.current_node_id,
                            runs.c.current_round_ordinal,
                        ).where(runs.c.run_id == run_id.value)
                    )
                    .mappings()
                    .one_or_none()
                )
                if run_record is None:
                    return QueryDurableStateCorrupt()
                head = int(run_record["last_event_sequence"])
                if after_sequence < 0 or after_sequence > head:
                    return EventHistoryCorrupt()
                records = tuple(
                    connection.execute(
                        _bounded_projection_select(
                            run_events,
                            self._projection_limit,
                            payload_columns=_EVENT_PAYLOAD_COLUMNS,
                            field_columns=_EVENT_FIELD_COLUMNS,
                        )
                        .where(
                            run_events.c.run_id == run_id.value,
                            run_events.c.event_sequence > after_sequence,
                        )
                        .order_by(run_events.c.event_sequence)
                        .limit(limit)
                    )
                    .mappings()
                    .all()
                )
                for record in records:
                    _validate_bounded_record(
                        record,
                        self._projection_limit,
                        payload_columns=_EVENT_PAYLOAD_COLUMNS,
                        field_columns=_EVENT_FIELD_COLUMNS,
                    )
                sequences = tuple(int(record["event_sequence"]) for record in records)
                expected_sequences = tuple(
                    range(after_sequence + 1, min(head, after_sequence + limit) + 1)
                )
                if sequences != expected_sequences:
                    return EventHistoryCorrupt()
                ended_the_run = _run_ending_event_predicate(
                    WorkflowFormatVersion(int(run_record["workflow_format_version"])),
                    NodeExecutionId.for_node(
                        run_id,
                        WorkflowRevisionHash(str(run_record["revision_hash"])),
                        str(run_record["current_node_id"]),
                        int(run_record["current_round_ordinal"]),
                    ),
                )
                terminal_sequences = tuple(
                    int(record["event_sequence"])
                    for record in records
                    if ended_the_run(_event_endpoint(record))
                )
                terminal = str(run_record["state"]) in {
                    RunState.COMPLETED.value,
                    RunState.FAILED.value,
                }
                reached_head = bool(sequences) and sequences[-1] == head
                if terminal_sequences not in ((), (head,)) or (
                    reached_head and ((terminal_sequences == (head,)) != terminal)
                ):
                    return EventHistoryCorrupt()
                events = tuple(
                    self._event_projection(
                        connection,
                        record,
                        WorkflowFormatVersion(
                            int(run_record["workflow_format_version"])
                        ),
                        self._projection_limit,
                    )
                    for record in records
                )
                return RunEventPage(
                    events,
                    terminal_sequences == (head,),
                )
        except ProjectionLimitExceeded:
            return ProjectionTooLarge()
        except (OperationalError, PoolTimeoutError):
            return ReadUnavailable()
        except (
            RevisionHashCollision,
            RunTransitionConflict,
            TypeError,
            ValueError,
            RuntimeError,
            DatabaseError,
        ):
            return QueryDurableStateCorrupt()

    def read_attention_event_page(
        self,
        after_run_id: RunId | None,
        after_sequence: int | None,
        limit: int,
        excluded_identities: tuple[tuple[RunId, int], ...],
    ) -> ReadAttentionEventPageResult:
        if type(limit) is not int or not 1 <= limit <= MAXIMUM_PAGE_ITEMS:
            raise ValueError(
                f"event page limit must be an integer from 1 to {MAXIMUM_PAGE_ITEMS}"
            )
        try:
            with self._connection() as connection:
                return load_attention_event_page(
                    connection,
                    after_run_id,
                    after_sequence,
                    limit,
                    self._projection_limit,
                    self._event_projection,
                    excluded_identities,
                )
        except ProjectionLimitExceeded:
            return ProjectionTooLarge()
        except (OperationalError, PoolTimeoutError):
            return ReadUnavailable()
        except (
            RevisionHashCollision,
            RunTransitionConflict,
            TypeError,
            ValueError,
            RuntimeError,
            DatabaseError,
        ):
            return QueryDurableStateCorrupt()

    @staticmethod
    def _event_projection(
        connection: Connection,
        record: Mapping[Any, Any],
        workflow_format_version: WorkflowFormatVersion,
        projection_limit: DurableProjectionLimit,
    ) -> PersistedRunEvent:
        event = event_from_record(record)
        if (
            event.event_kind is RunEventKind.AGENT_FAILED
            and workflow_format_version not in _AGENT_FAILURE_FORMATS
        ):
            raise RunTransitionConflict("V1 run carries an agent failure event")
        if event.event_kind is RunEventKind.AGENT_FAILED and event.payload not in {
            *(code.value.encode("ascii") for code in AgentAttemptFailureCode),
            AgentExecutionRefusal.EXECUTOR_BINDING_UNAVAILABLE.value.encode("ascii"),
        }:
            raise RunTransitionConflict("agent failure event payload is not canonical")
        node_receipt_reason = (
            AgentExecutionRefusal.EXECUTOR_BINDING_UNAVAILABLE.value
            if event.event_kind is RunEventKind.AGENT_FAILED
            and event.payload
            == AgentExecutionRefusal.EXECUTOR_BINDING_UNAVAILABLE.value.encode("ascii")
            else (
                _node_receipt_refusal(connection, event.node_execution_id)
                if event.event_kind is RunEventKind.AGENT_FAILED
                else None
            )
        )
        if event.event_kind not in {
            RunEventKind.ACTION_RECONCILIATION_RESOLVED,
            RunEventKind.ACTION_COMPLETED,
        }:
            return PersistedRunEvent(
                event, None, workflow_format_version, node_receipt_reason
            )
        logical_key = event.receipt_logical_key
        if logical_key is None:
            raise RunTransitionConflict("receipt event has no logical key")
        receipt_record = (
            connection.execute(
                _bounded_projection_select(
                    effect_receipts,
                    projection_limit,
                    payload_columns=_RECEIPT_PAYLOAD_COLUMNS,
                    field_columns=_RECEIPT_FIELD_COLUMNS,
                ).where(effect_receipts.c.logical_key == logical_key.value)
            )
            .mappings()
            .one_or_none()
        )
        if receipt_record is None:
            raise RunTransitionConflict("receipt event has no durable receipt")
        _validate_bounded_record(
            receipt_record,
            projection_limit,
            payload_columns=_RECEIPT_PAYLOAD_COLUMNS,
            field_columns=_RECEIPT_FIELD_COLUMNS,
        )
        receipt = receipt_from_record(receipt_record)
        if (
            receipt.intent.binding.run_id != event.run_id
            or receipt.intent.binding.workflow_revision_hash != event.revision_hash
            or receipt.result.payload_hash != event.receipt_result_hash
        ):
            raise RunTransitionConflict("receipt event binding disagrees")
        return PersistedRunEvent(event, receipt, workflow_format_version)
