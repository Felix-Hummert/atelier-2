from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from decimal import Decimal
from typing import Any, assert_never

import sqlalchemy as sa
from dbos import DBOSClient, EnqueueOptions
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.agent_effect_grants import (
    agent_node_redeems_platform_effect,
)
from atelier2.adapters.dbos.artifact_store import keep_artifact, read_stored_artifact
from atelier2.adapters.dbos.effect_store import (
    intent_snapshot_from_record,
    receipt_from_record,
)
from atelier2.adapters.dbos.instants import record_attempt_ended, record_attempt_started
from atelier2.adapters.dbos.names import (
    CANCELLATION_WORKFLOW_NAME,
    QUEUE_NAME,
    REPLACEMENT_WORKFLOW_NAME,
)
from atelier2.adapters.dbos.node_records import keep_node_receipt
from atelier2.adapters.dbos.run_store import (
    AgentReceiptConflict,
    ToolRedemptionConflict,
    _agent_receipt_v2_from_record,
    _agent_receipt_v2_values,
    _tool_redemption_from_record,
    _tool_redemption_values,
    load_kept_value,
    load_node_outputs,
    load_published_schema_document,
    load_run_inputs,
)
from atelier2.adapters.dbos.run_transitions import (
    RunTransitionConflict,
    _commit_event,
    _insert_event,
    commit_wait_cancelled,
    lift_started_run,
    load_graph,
    load_run,
)
from atelier2.adapters.dbos.schema import (
    agent_attempt_receipts_v3,
    agent_attempts,
    agent_receipts_v2,
    effect_intents,
    effect_receipts,
    run_events,
    runs,
    tool_redemptions,
    wait_answers,
)
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.adapters.dbos.uncontinuable_runs import live_driver_workflow_ids
from atelier2.adapters.dbos.workflow_ids import (
    cancellation_workflow_id_for,
    driving_workflow_ids,
    replacement_workflow_id_for,
)
from atelier2.application.compose_node_job import (
    NodeJobCompositionVersion,
    OutputSchemaRepair,
    node_job,
)
from atelier2.contracts.agent_attempts import (
    AGENT_ATTEMPT_ORDINAL,
    REPLACEMENT_AGENT_ATTEMPT_ORDINAL,
    TERMINAL_AGENT_ATTEMPT_STATES,
    AgentAttempt,
    AgentAttemptCancellation,
    AgentAttemptCancellationDisposition,
    AgentAttemptFailureCode,
    AgentAttemptId,
    AgentAttemptProcessPhase,
    AgentAttemptRedriveState,
    AgentAttemptReplacement,
    AgentAttemptState,
    AgentProcessOwnerId,
    CancelAgentAttemptRequest,
    OutputSchemaRefusalReceipt,
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
    RunnerProcessBoundaryFailure,
    RunnerProviderFailure,
    RunnerProviderResult,
    RunnerTerminalEvidenceAckTombstone,
    RunnerTerminalEvidenceEnvelope,
    RunnerTerminalEvidenceHash,
    WatchdogGenerationId,
)
from atelier2.contracts.agent_refusals import (
    AGENT_REFUSAL_SCHEMA,
    agent_refusal_reason,
)
from atelier2.contracts.agent_transcripts import AttemptTranscript
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_FIELD_CHARACTERS,
    MAXIMUM_AGENT_OUTPUT_BYTES_V2,
    AgentExecutionRequestHash,
    AgentExecutionRequestV2,
    AgentExecutionResult,
    AgentExecutorOperationalIdentity,
    AgentReceiptHash,
    AgentReceiptV2,
)
from atelier2.contracts.artifacts import Artifact, ArtifactHash
from atelier2.contracts.effects import EffectIntentState
from atelier2.contracts.executions import (
    AgentAttemptExecution,
    AgentExecutionRefusal,
    NodeExecutionId,
    RunEvent,
    RunEventAgentAttemptBinding,
    RunEventCancellationBinding,
    RunEventKind,
    WaitAnswerState,
    logical_effect_key_for_node,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.node_records_v3 import (
    NodeArtifact,
    NodeReceiptReason,
    PersistedReceiptDisposition,
    node_receipt_reason,
)
from atelier2.contracts.pages import PageLimit
from atelier2.contracts.revisions_v3 import PublishedRevisionHash
from atelier2.contracts.run_bindings import RunV2, RunV3
from atelier2.contracts.run_cancellations import (
    CancelRunRequest,
    RunCancelCommandId,
    is_operator_run_cancel,
)
from atelier2.contracts.run_projections import RunCancellationRefusal
from atelier2.contracts.runs import (
    TERMINAL_RUN_STATES,
    RunId,
    RunState,
    WorkflowRevisionHash,
)
from atelier2.contracts.schemas_v3 import (
    InstanceRefused,
    SchemaRefused,
    read_instance_document,
    read_schema_document,
)
from atelier2.contracts.tool_grants_v3 import ToolRedemptionReceipt
from atelier2.contracts.verdicts import Verdict, read_verdict
from atelier2.contracts.workflows import (
    NodeCompletion,
    RunCompletes,
    RunContinues,
    completion_after_node,
)
from atelier2.contracts.workflows_v3 import (
    AgentNodeV3,
    NodeOutput,
    WorkflowGraphV3,
    verdict_condition_of,
)
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationAccepted,
    AgentAttemptCancellationCommandConflict,
    AgentAttemptCancellationNotCurrent,
    AgentAttemptCancellationResult,
    AgentAttemptCancellationRunMissing,
    AgentAttemptCancellationStale,
    AgentAttemptCancellationTargetMissing,
    AgentAttemptCancellationTerminalConflict,
    AgentAttemptClaimedByThisCall,
    AgentAttemptClaimResult,
    AgentAttemptFailed,
    AgentAttemptPossiblyRan,
    AgentAttemptReplacementNotAllowed,
    AgentAttemptSucceeded,
    AgentExecutorBindingRefusalFenced,
    AgentExecutorBindingRefusalNeedsPreparedCleanup,
    AgentExecutorBindingRefusalResult,
    AgentExecutorBindingRefusalWritten,
    RunCancellationAccepted,
    RunCancellationCommandConflict,
    RunCancellationEndedRun,
    RunCancellationNotCancellable,
    RunCancellationOvertakenBySuccess,
    RunCancellationResult,
    RunCancellationRunMissing,
    RunCancellationTerminalRetry,
    RunnerTerminalEvidenceCommitRefused,
    RunnerTerminalEvidenceCommitResult,
    RunnerTerminalEvidenceCommitted,
    RunnerTerminalEvidenceRefusal,
)
from atelier2.ports.durable_runs import DurableStateCorrupt


def attempt_from_record(record: Mapping[Any, Any]) -> AgentAttempt:
    """Rebuild the typed attempt one `agent_attempts` row records.

    This module owns the row-to-attempt mapping, so a second reader that needs an
    attempt back from its durable row -- the live-GitHub startup scan asking which
    workflow still drives it -- reads it here rather than re-deriving the shape.
    """
    try:
        failure = record["failure_code"]
        receipt = record["receipt_hash"]
        owner = record["process_owner_id"]
        generation = record["watchdog_generation_id"]
        command_id = record["cancellation_command_id"]
        disposition = record["cancellation_disposition"]
        runner_manifest = record["runner_manifest_id"]
        runner_generation = record["runner_generation_id"]
        runner_invocation = record["runner_invocation_id"]
        runner_evidence_hash = record["runner_terminal_evidence_hash"]
        transcript = record["transcript_artifact_hash"]
        cancellation = (
            None
            if command_id is None
            else AgentAttemptCancellation(
                str(command_id),
                int(record["cancellation_expected_state_version"]),
                AgentAttemptReplacement(str(record["replacement"])),
                AgentAttemptRedriveState(str(record["redrive_state"])),
                (
                    None
                    if disposition is None
                    else AgentAttemptCancellationDisposition(str(disposition))
                ),
            )
        )
        return AgentAttempt(
            AgentAttemptId(str(record["attempt_id"])),
            NodeExecutionId(str(record["node_execution_id"])),
            AgentExecutionRequestHash(str(record["request_hash"])),
            AgentExecutorOperationalIdentity(
                str(record["executor_operational_identity"])
            ),
            RunId(str(record["run_id"])),
            WorkflowRevisionHash(str(record["workflow_revision_hash"])),
            str(record["node_id"]),
            int(record["attempt_ordinal"]),
            AgentAttemptState(str(record["state"])),
            int(record["state_version"]),
            None if failure is None else AgentAttemptFailureCode(str(failure)),
            None if receipt is None else AgentReceiptHash(str(receipt)),
            AgentAttemptProcessPhase(str(record["process_phase"])),
            None if owner is None else AgentProcessOwnerId(str(owner)),
            None if generation is None else WatchdogGenerationId(str(generation)),
            cancellation,
            None if runner_manifest is None else RunnerManifestId(str(runner_manifest)),
            None
            if runner_generation is None
            else RunnerGenerationId(str(runner_generation)),
            None
            if runner_invocation is None
            else RunnerInvocationId(str(runner_invocation)),
            None
            if runner_evidence_hash is None
            else RunnerTerminalEvidenceHash(str(runner_evidence_hash)),
            RunnerEvidenceAcceptancePhase(
                str(record["runner_evidence_acceptance_phase"])
            ),
            None if transcript is None else ArtifactHash(str(transcript)),
        )
    except (TypeError, ValueError) as error:
        raise RunTransitionConflict(
            "durable agent attempt binding disagrees"
        ) from error


def _attempt_values(attempt: AgentAttempt) -> dict[str, object]:
    return {
        "attempt_id": attempt.attempt_id.value,
        "node_execution_id": attempt.node_execution_id.value,
        "request_hash": attempt.request_hash.value,
        "executor_operational_identity": attempt.executor_operational_identity.value,
        "run_id": attempt.run_id.value,
        "workflow_revision_hash": attempt.workflow_revision_hash.value,
        "node_id": attempt.node_id,
        "attempt_ordinal": attempt.attempt_ordinal,
        "state": attempt.state.value,
        "state_version": attempt.state_version,
        "process_phase": attempt.process_phase.value,
        "process_owner_id": (
            None if attempt.process_owner_id is None else attempt.process_owner_id.value
        ),
        "watchdog_generation_id": (
            None
            if attempt.watchdog_generation_id is None
            else attempt.watchdog_generation_id.value
        ),
        "cancellation_command_id": (
            None if attempt.cancellation is None else attempt.cancellation.command_id
        ),
        "cancellation_expected_state_version": (
            None
            if attempt.cancellation is None
            else attempt.cancellation.expected_attempt_state_version
        ),
        "replacement": (
            None
            if attempt.cancellation is None
            else attempt.cancellation.replacement.value
        ),
        "redrive_state": (
            None
            if attempt.cancellation is None
            else attempt.cancellation.redrive_state.value
        ),
        "cancellation_disposition": (
            None
            if attempt.cancellation is None or attempt.cancellation.disposition is None
            else attempt.cancellation.disposition.value
        ),
        "cancellation_workflow_id": None,
        "failure_code": (
            None if attempt.failure_code is None else attempt.failure_code.value
        ),
        "receipt_hash": (
            None if attempt.receipt_hash is None else attempt.receipt_hash.value
        ),
        "runner_manifest_id": (
            None
            if attempt.runner_manifest_id is None
            else attempt.runner_manifest_id.value
        ),
        "runner_generation_id": (
            None
            if attempt.runner_generation_id is None
            else attempt.runner_generation_id.value
        ),
        "runner_invocation_id": (
            None
            if attempt.runner_invocation_id is None
            else attempt.runner_invocation_id.value
        ),
        "runner_terminal_evidence_hash": (
            None
            if attempt.runner_terminal_evidence_hash is None
            else attempt.runner_terminal_evidence_hash.value
        ),
        "runner_evidence_acceptance_phase": (
            attempt.runner_evidence_acceptance_phase.value
        ),
        "transcript_artifact_hash": (
            None
            if attempt.transcript_artifact_hash is None
            else attempt.transcript_artifact_hash.value
        ),
    }


def _prepared_attempt(execution: AgentAttemptExecution) -> AgentAttempt:
    request = execution.request
    return AgentAttempt(
        execution.attempt_id,
        request.node_execution_id,
        request.request_hash,
        request.executor_operational_identity,
        request.run_id,
        request.workflow_revision_hash,
        request.node_id,
        execution.ordinal,
        AgentAttemptState.PREPARED,
        0,
    )


def _load_attempt(session: Any, attempt_id: AgentAttemptId) -> AgentAttempt:
    record = (
        session.execute(
            sa.select(agent_attempts).where(
                agent_attempts.c.attempt_id == attempt_id.value
            )
        )
        .mappings()
        .one_or_none()
    )
    if record is None:
        raise RunTransitionConflict("agent attempt is missing")
    return attempt_from_record(record)


def _agent_node_for_attempt(graph: WorkflowGraphV3, node_id: str) -> AgentNodeV3:
    """Return the declared Agent node an attempt is allowed to name."""
    node = graph.node(node_id)
    if not isinstance(node, AgentNodeV3):
        raise RunTransitionConflict("agent attempt request differs from durable graph")
    return node


def load_output_schema_refusal_receipt(
    connection: Any,
    attempt_id: AgentAttemptId,
    *,
    expected_node_execution_id: NodeExecutionId,
    expected_attempt_ordinal: int,
    expected_schema_revision: PublishedRevisionHash,
) -> OutputSchemaRefusalReceipt | None:
    """Load one immutable refusal row and prove every identity it carries.

    A receipt is permission to compose the repair request.  Returning a partly
    checked row would turn a loose reason string back into that permission, so
    this owner validates the attempt it belongs to, the schema and bytes it
    judged, and the receipt hash derived from the complete row.
    """
    attempt = (
        connection.execute(
            sa.select(agent_attempts).where(
                agent_attempts.c.attempt_id == attempt_id.value
            )
        )
        .mappings()
        .one_or_none()
    )
    if attempt is None:
        raise RunTransitionConflict("output-schema refusal attempt is missing")
    if (
        str(attempt["attempt_id"]) != attempt_id.value
        or str(attempt["node_execution_id"]) != expected_node_execution_id.value
        or int(attempt["attempt_ordinal"]) != expected_attempt_ordinal
    ):
        raise RunTransitionConflict(
            "output-schema refusal receipt belongs to another attempt"
        )
    record = (
        connection.execute(
            sa.select(agent_attempt_receipts_v3).where(
                agent_attempt_receipts_v3.c.attempt_id == attempt_id.value
            )
        )
        .mappings()
        .one_or_none()
    )
    if record is None:
        return None
    receipt = OutputSchemaRefusalReceipt(
        AgentAttemptId(str(record["attempt_id"])),
        str(record["reason"]),
        PublishedRevisionHash(str(record["schema_revision_hash"])),
        Sha256Hash(str(record["value_hash"])),
        (
            None
            if record["artifact_hash"] is None
            else ArtifactHash(str(record["artifact_hash"]))
        ),
    )
    if (
        receipt.attempt_id != attempt_id
        or receipt.schema_revision != expected_schema_revision
        or receipt.receipt_hash.value != str(record["receipt_hash"])
    ):
        raise RunTransitionConflict("output-schema refusal receipt binding differs")
    if receipt.artifact_hash is None:
        if receipt.value_hash != Sha256Hash.of(b""):
            raise RunTransitionConflict(
                "nonempty output-schema refusal has no artifact"
            )
    else:
        if receipt.artifact_hash.value != receipt.value_hash.value:
            raise RunTransitionConflict(
                "output-schema refusal artifact differs from its value hash"
            )
        artifact = read_stored_artifact(connection, receipt.artifact_hash)
        if artifact is None or Sha256Hash.of(artifact.content) != receipt.value_hash:
            raise RunTransitionConflict(
                "output-schema refusal artifact is missing or differs"
            )
    return receipt


def load_prior_output_schema_refusal_receipt(
    connection: Any,
    *,
    target_attempt_id: AgentAttemptId,
    target_node_execution_id: NodeExecutionId,
    target_attempt_ordinal: int,
    expected_schema_revision: PublishedRevisionHash,
) -> OutputSchemaRefusalReceipt | None:
    """The exact ordinal-one receipt strictly before a repair candidate."""
    if target_attempt_ordinal == AGENT_ATTEMPT_ORDINAL:
        return None
    if target_attempt_ordinal != REPLACEMENT_AGENT_ATTEMPT_ORDINAL:
        raise RunTransitionConflict("repair target ordinal is outside the vocabulary")
    prior_attempt_id = connection.scalar(
        sa.select(agent_attempts.c.attempt_id).where(
            agent_attempts.c.node_execution_id == target_node_execution_id.value,
            agent_attempts.c.attempt_ordinal == AGENT_ATTEMPT_ORDINAL,
        )
    )
    if prior_attempt_id is None:
        return None
    receipt = load_output_schema_refusal_receipt(
        connection,
        AgentAttemptId(str(prior_attempt_id)),
        expected_node_execution_id=target_node_execution_id,
        expected_attempt_ordinal=AGENT_ATTEMPT_ORDINAL,
        expected_schema_revision=expected_schema_revision,
    )
    if receipt is not None and receipt.attempt_id == target_attempt_id:
        raise RunTransitionConflict("repair receipt is not strictly prior")
    return receipt


def compose_agent_node_job_for_attempt(
    node: AgentNodeV3,
    orders: tuple[Any, ...],
    results: tuple[Any, ...],
    *,
    base_composition_version: NodeJobCompositionVersion,
    target_node_execution_id: NodeExecutionId,
    target_attempt_ordinal: int,
    prior_refusal_receipt: OutputSchemaRefusalReceipt | None,
) -> bytes:
    """Compose one attempt from an explicit base and a fully validated receipt."""
    if base_composition_version not in {
        NodeJobCompositionVersion.LEGACY,
        NodeJobCompositionVersion.CURRENT,
    }:
        raise ValueError("an agent job base version must be legacy or current")
    if prior_refusal_receipt is not None:
        if target_attempt_ordinal != REPLACEMENT_AGENT_ATTEMPT_ORDINAL:
            raise RunTransitionConflict("repair receipt is not prior to its target")
        composition_version = NodeJobCompositionVersion.OUTPUT_SCHEMA_REPAIR
        repair = OutputSchemaRepair(prior_refusal_receipt.reason)
    else:
        composition_version = base_composition_version
        repair = None
    if not isinstance(target_node_execution_id, NodeExecutionId):
        raise TypeError("attempt composition requires a typed execution identity")
    return node_job(
        node.instruction,
        orders,
        results,
        composition_version=composition_version,
        output_schema_repair=repair,
    ).encode("utf-8")


def _validate_request(
    session: Any,
    request: AgentExecutionRequestV2,
    target_attempt_id: AgentAttemptId,
    target_attempt_ordinal: int,
) -> tuple[RunV2 | RunV3, WorkflowGraphV3]:
    """The run and graph one attempt request must exactly describe.

    A V3 agent node runs here too. Its role binding, its attempt identity and its
    provider contract are the ones V2 already carries, so admitting it is a wider
    door rather than a second path -- what a V3 run does differently begins after
    the provider answers, where the receipt chain is written.

    Only bound runs pass: a V1 run has no role matrix to attempt against. The
    graph is what decides how a node is read, and the run head is what decides
    where the run stands, so neither format is asked to answer for the other.
    """
    run = load_run(session, request.run_id)
    graph = load_graph(session, request.workflow_revision_hash)
    if not isinstance(run, (RunV2, RunV3)) or not isinstance(graph, WorkflowGraphV3):
        raise RunTransitionConflict("agent attempt requires a bound run")
    if run.revision_hash != request.workflow_revision_hash:
        raise RunTransitionConflict("agent attempt request names another revision")
    node = _agent_node_for_attempt(graph, request.node_id)
    # Recomputed from durable truth rather than trusted: what the node's author
    # wrote, plus the orders this run was started with, through the one owner
    # that decides what an agent is handed. A second spelling here would let a
    # request claim a job the document and the run never agreed on.
    schema_revision = PublishedRevisionHash(node.outputs[0].schema_reference.revision)
    prior_receipt = load_prior_output_schema_refusal_receipt(
        session,
        target_attempt_id=target_attempt_id,
        target_node_execution_id=request.node_execution_id,
        target_attempt_ordinal=target_attempt_ordinal,
        expected_schema_revision=schema_revision,
    )
    orders = load_run_inputs(session, request.run_id, node)
    results = load_node_outputs(
        session,
        request.run_id,
        request.workflow_revision_hash,
        graph,
        node,
        request.round_ordinal,
    )
    authored_job = compose_agent_node_job_for_attempt(
        node,
        orders,
        results,
        base_composition_version=NodeJobCompositionVersion.CURRENT,
        target_node_execution_id=request.node_execution_id,
        target_attempt_ordinal=target_attempt_ordinal,
        prior_refusal_receipt=prior_receipt,
    )
    if prior_receipt is None and authored_job != request.job_bytes:
        authored_job = compose_agent_node_job_for_attempt(
            node,
            orders,
            results,
            base_composition_version=NodeJobCompositionVersion.LEGACY,
            target_node_execution_id=request.node_execution_id,
            target_attempt_ordinal=target_attempt_ordinal,
            prior_refusal_receipt=None,
        )
    if (
        node.role != request.resolved_binding.role.value
        or authored_job != request.job_bytes
    ):
        raise RunTransitionConflict("agent attempt request differs from durable graph")
    durable_binding = next(
        (
            binding
            for binding in run.agent_bindings
            if binding.role == request.resolved_binding.role
        ),
        None,
    )
    if durable_binding != request.resolved_binding:
        raise RunTransitionConflict(
            "agent attempt request differs from durable binding"
        )
    return run, graph


def _output_schema_repair_request(
    connection: Any,
    request: AgentExecutionRequestV2,
    graph: WorkflowGraphV3,
    node: AgentNodeV3,
    prior_receipt: OutputSchemaRefusalReceipt,
) -> AgentExecutionRequestV2:
    target_ordinal = REPLACEMENT_AGENT_ATTEMPT_ORDINAL
    job = compose_agent_node_job_for_attempt(
        node,
        load_run_inputs(connection, request.run_id, node),
        load_node_outputs(
            connection,
            request.run_id,
            request.workflow_revision_hash,
            graph,
            node,
            request.round_ordinal,
        ),
        base_composition_version=NodeJobCompositionVersion.CURRENT,
        target_node_execution_id=request.node_execution_id,
        target_attempt_ordinal=target_ordinal,
        prior_refusal_receipt=prior_receipt,
    )
    return AgentExecutionRequestV2(
        request.node_execution_id,
        request.run_id,
        request.workflow_revision_hash,
        request.node_id,
        request.resolved_binding,
        request.executor_operational_identity,
        job,
        request.declared_output_schema_bytes,
        request.round_ordinal,
        request.maximum_assistant_turns,
    )


def _require_attempt_binding(
    attempt: AgentAttempt, execution: AgentAttemptExecution
) -> None:
    request = execution.request
    if (
        attempt.attempt_id != execution.attempt_id
        or attempt.node_execution_id != request.node_execution_id
        or attempt.request_hash != request.request_hash
        or attempt.executor_operational_identity
        != request.executor_operational_identity
        or attempt.run_id != request.run_id
        or attempt.workflow_revision_hash != request.workflow_revision_hash
        or attempt.node_id != request.node_id
        or attempt.attempt_ordinal != execution.ordinal
    ):
        raise RunTransitionConflict("durable agent attempt differs from exact retry")


def _require_completed_attempt_head(
    connection: Any,
    run: RunV2 | RunV3,
    request: AgentExecutionRequestV2,
    completion: NodeCompletion,
    completion_is_deferred: bool,
) -> None:
    if completion_is_deferred:
        if (
            run.state is not RunState.STARTED
            or run.current_node_id != request.node_id
            or run.current_round_ordinal != request.round_ordinal
        ):
            _require_confirmed_effect_receipt_for_completed_attempt(connection, request)
        else:
            return
    match completion:
        case RunContinues(node_id, round_ordinal):
            if (
                run.state is not RunState.STARTED
                or run.current_node_id != node_id
                or run.current_round_ordinal != round_ordinal
            ):
                raise RunTransitionConflict(
                    "successful attempt has no exact successor transition"
                )
        case RunCompletes():
            if (
                run.state is not RunState.COMPLETED
                or run.current_node_id != request.node_id
            ):
                raise RunTransitionConflict(
                    "successful terminal attempt has no exact completed transition"
                )
        case _ as unreachable:
            assert_never(unreachable)


def _require_confirmed_effect_receipt_for_completed_attempt(
    connection: Any, request: AgentExecutionRequestV2
) -> None:
    """Prove that this exact effect grant settled before accepting its replay.

    An attempt whose effect continuation already moved the run is safe to replay
    only when the intent for this execution reached CONFIRMED and its receipt is
    present. The intent also has to carry this attempt's durable output, otherwise
    an unrelated confirmed effect could make a torn head look like a continuation.
    """
    logical_key = logical_effect_key_for_node(
        request.run_id,
        request.workflow_revision_hash,
        request.node_id,
        request.round_ordinal,
    )
    intent_record = (
        connection.execute(
            sa.select(effect_intents).where(
                effect_intents.c.logical_key == logical_key.value
            )
        )
        .mappings()
        .one_or_none()
    )
    receipt_record = (
        connection.execute(
            sa.select(effect_receipts).where(
                effect_receipts.c.logical_key == logical_key.value
            )
        )
        .mappings()
        .one_or_none()
    )
    output = connection.execute(
        sa.select(
            agent_receipts_v2.c.output_bytes, agent_receipts_v2.c.output_hash
        ).where(
            agent_receipts_v2.c.node_execution_id == request.node_execution_id.value
        )
    ).one_or_none()
    if intent_record is None or receipt_record is None or output is None:
        raise RunTransitionConflict(
            "successful effect-grant attempt has no exact confirmed effect receipt"
        )
    intent_snapshot = intent_snapshot_from_record(intent_record)
    receipt = receipt_from_record(receipt_record)
    if (
        intent_snapshot.state is not EffectIntentState.CONFIRMED
        or receipt.intent != intent_snapshot.intent
        or bytes(intent_snapshot.intent.request.payload) != bytes(output.output_bytes)
        or str(intent_snapshot.intent.request.request_hash.value)
        != str(output.output_hash)
    ):
        raise RunTransitionConflict(
            "successful effect-grant attempt has no exact confirmed effect receipt"
        )


def _agent_platform_effect_completion_is_deferred(
    connection: Any, node: AgentNodeV3
) -> bool:
    """Whether this node's kept output must wait for platform-effect settlement.

    An effect grant redeems only after the agent output has become durable, but
    the run may not leave the node until that redemption has a receipt. The
    grant document is the same pinned revision the node binding already read;
    a missing or invalid document is therefore durable corruption, not an
    absence that could make a completed run honest.
    """
    return agent_node_redeems_platform_effect(connection, node)


def _kept_transcript_values(
    connection: Any, transcript: AttemptTranscript | None
) -> dict[str, object]:
    """The attempt column this transcript sets, its bytes kept under that address.

    Keeping the material and naming it happen in the caller's own transaction,
    so no attempt can point at a transcript this store never got. The bytes
    arrive already bounded and redacted -- `AttemptTranscript` is the only way
    to make one -- so nothing here judges them a second time.
    """

    if transcript is None:
        return {}
    artifact = Artifact(transcript.document)
    keep_artifact(connection, artifact)
    return {
        agent_attempts.c.transcript_artifact_hash.name: artifact.artifact_hash.value
    }


def _fail_current_attempt(
    connection: Any,
    execution: AgentAttemptExecution,
    durable: AgentAttempt,
    failure: AgentAttemptFailureCode,
    receipt_reason: str,
    schema_revision: PublishedRevisionHash | None = None,
    judged_value: bytes | None = None,
    runner_evidence_hash: RunnerTerminalEvidenceHash | None = None,
    transcript: AttemptTranscript | None = None,
    redemption: ToolRedemptionReceipt | None = None,
    terminal_node_failure: bool = True,
) -> AgentAttemptFailed:
    """One durable failure seam for every way an armed attempt ends badly.

    The attempt turns `FAILED` under its named code and the `AGENT_FAILED` event
    carries that code. A terminal failure ends the run on the same node; the
    first output-schema refusal instead keeps the run `STARTED` while the exact
    repair is enqueued in this transaction. `receipt_reason` is the words of
    whoever judged this ending -- a compact schema-refusal diagnosis where an
    answer was refused, the supervision where a process died -- and every way
    through here carries one, because a failure whose reason is nowhere is the
    silent death this seam exists to end. A schema judgment also keeps the
    identity it judged; a process that died judged nothing, so those fields stay
    honestly empty.

    `judged_value` is the exact bytes that judgment read, and it arrives as
    bytes rather than as a hash because the seam that records the verdict is the
    only place that can also keep the evidence: the receipt's value hash is
    derived here, and the same bytes are held as an artifact under exactly that
    address in the same transaction. Without them a refused episode says only
    that something was refused and never what was written -- the receipt names a
    hash nothing resolves, and the one thing an operator needs to read is gone
    for good. Empty bytes are the exception: there is nothing to keep, and the
    hash of nothing already says so.

    `transcript` is what the executor decoded of the provider's own stream on
    the way to this ending, and it is kept here for the same reason: the ending
    an operator most needs to read is the one nobody can explain, and a failure
    whose steps are nowhere is that silence again one level down (#733).

    `redemption` is present only where the attempt's granted check *passed* and
    the attempt failed afterwards -- today, where the work could not be kept. It
    is written in this same transaction because it is evidence of something that
    really happened: dropping it because the ending turned out badly would erase
    a command that ran and exited zero, and leave an operator reading a failure
    with no way to tell whether the project's own check had ever been satisfied.
    Endings that failed *because* the check failed pass none, and there is
    nothing to write (#642).
    """
    request = execution.request
    attempt_id = execution.attempt_id
    value_hash = None if judged_value is None else Sha256Hash.of(judged_value)
    if judged_value:
        keep_artifact(connection, Artifact(judged_value))
    _keep_tool_redemption(connection, execution, redemption)
    if terminal_node_failure:
        keep_node_receipt(
            connection,
            request.node_execution_id,
            PersistedReceiptDisposition.FAILED,
            receipt_reason,
            schema_revision=schema_revision,
            value_hash=value_hash,
        )
    values: dict[str, object] = {
        "state": AgentAttemptState.FAILED.value,
        "state_version": durable.state_version + 1,
        "failure_code": failure.value,
        **_kept_transcript_values(connection, transcript),
    }
    if runner_evidence_hash is not None:
        values.update(
            runner_terminal_evidence_hash=runner_evidence_hash.value,
            runner_evidence_acceptance_phase=(
                RunnerEvidenceAcceptancePhase.CORE_COMMITTED.value
            ),
        )
        if durable.state is AgentAttemptState.CANCEL_REQUESTED:
            values.update(
                cancellation_command_id=None,
                cancellation_expected_state_version=None,
                replacement=None,
                redrive_state=None,
                cancellation_disposition=None,
                cancellation_workflow_id=None,
            )
    updated = connection.execute(
        agent_attempts.update()
        .where(
            agent_attempts.c.attempt_id == attempt_id.value,
            agent_attempts.c.state == durable.state.value,
            agent_attempts.c.state_version == durable.state_version,
        )
        .values(**values)
    )
    if updated.rowcount != 1:
        raise RunTransitionConflict("agent failure lost its attempt CAS")
    record_attempt_ended(connection, attempt_id.value)
    durable_failure = _load_attempt(connection, attempt_id)
    _commit_event(
        connection,
        request.run_id,
        request.workflow_revision_hash,
        request.node_id,
        RunEventKind.AGENT_FAILED,
        failure.value.encode("ascii"),
        RunState.STARTED,
        RunState.FAILED if terminal_node_failure else RunState.STARTED,
        request.node_id,
        terminal=terminal_node_failure,
        agent_attempt_id=attempt_id,
        attempt_ordinal=execution.ordinal,
        round_ordinal=request.round_ordinal,
        target_round_ordinal=request.round_ordinal,
    )
    return AgentAttemptFailed(durable_failure)


def _declared_output_schema_refusal(
    session: Any, node_id: str, declared: NodeOutput, payload: bytes
) -> InstanceRefused | None:
    """Read one declared output against its pinned schema without losing its shape."""
    document = load_published_schema_document(
        session, declared.schema_reference.revision
    )
    if document is None:
        raise RunTransitionConflict(
            f"the schema node {node_id!r} pinned for output "
            f"{declared.name!r} is absent from the store"
        )
    schema = read_schema_document(document)
    if isinstance(schema, SchemaRefused):
        raise RunTransitionConflict(
            f"the schema node {node_id!r} pinned for output "
            f"{declared.name!r} is not one: {schema}"
        )
    # The byte bound belongs to the route the value arrived by
    # (schemas_v3.py's read_instance_document docstring), and an agent output
    # arrives through the provider frame, not an inline order: its route bound
    # is MAXIMUM_AGENT_OUTPUT_BYTES_V2, not read_instance_document's inline
    # default. #901 slice 5's V3 schema validation newly applied the inline
    # door's bound to outputs the provider frame legally admits, refusing a
    # legal answer before the schema itself was ever consulted.
    verdict = read_instance_document(
        payload, schema, maximum_bytes=MAXIMUM_AGENT_OUTPUT_BYTES_V2
    )
    return verdict if isinstance(verdict, InstanceRefused) else None


def _value_at_schema_violation(payload: bytes, pointer: str | None) -> object:
    """Read the one JSON value whose repr JSON Schema put in its diagnostic."""
    value: object = json.loads(payload.decode("utf-8"), parse_float=Decimal)
    if pointer is None:
        return value
    for escaped_part in pointer.removeprefix("/").split("/"):
        part = escaped_part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            value = value[int(part)]
        elif isinstance(value, dict):
            value = value[part]
        else:
            raise TypeError(
                f"schema violation pointer {pointer!r} does not address its value"
            )
    return value


def _schema_rule_without_rejected_value(reason: str, rejected_value: object) -> str:
    """Remove exactly JSON Schema's repr of the rejected value from its rule."""
    rendered_value = repr(rejected_value)
    return reason.replace(rendered_value, "", 1).strip()


def _compact_schema_refusal(refusal: InstanceRefused, payload: bytes) -> str:
    """Name the schema's place and rule without embedding rejected output."""
    violation = refusal.violation
    if violation is None:
        words = str(refusal)
    else:
        place = "the value itself" if violation.pointer is None else violation.pointer
        words = (
            f"{refusal.refusal.value}: {place}: "
            f"{_schema_rule_without_rejected_value(violation.reason, _value_at_schema_violation(payload, violation.pointer))}"
        )
    maximum_words = (
        MAXIMUM_AGENT_FIELD_CHARACTERS
        - len(NodeReceiptReason.OUTPUT_SCHEMA_REFUSED.value)
        - len(": ")
    )
    return words[:maximum_words]


def _store_output_schema_refusal_receipt(
    connection: Any,
    attempt_id: AgentAttemptId,
    reason: str,
    schema_revision: PublishedRevisionHash,
    value: bytes,
) -> OutputSchemaRefusalReceipt:
    artifact = None if value == b"" else Artifact(value)
    if artifact is not None:
        keep_artifact(connection, artifact)
    receipt = OutputSchemaRefusalReceipt(
        attempt_id,
        reason,
        schema_revision,
        Sha256Hash.of(value),
        None if artifact is None else artifact.artifact_hash,
    )
    connection.execute(
        agent_attempt_receipts_v3.insert()
        .prefix_with("OR IGNORE")
        .values(
            attempt_id=receipt.attempt_id.value,
            reason=receipt.reason,
            schema_revision_hash=receipt.schema_revision.value,
            value_hash=receipt.value_hash.value,
            artifact_hash=(
                None if receipt.artifact_hash is None else receipt.artifact_hash.value
            ),
            receipt_hash=receipt.receipt_hash.value,
        )
    )
    attempt = _load_attempt(connection, attempt_id)
    durable = load_output_schema_refusal_receipt(
        connection,
        attempt_id,
        expected_node_execution_id=attempt.node_execution_id,
        expected_attempt_ordinal=attempt.attempt_ordinal,
        expected_schema_revision=schema_revision,
    )
    if durable is None or durable != receipt:
        raise RunTransitionConflict("durable output-schema refusal receipt differs")
    return durable


def _kept_verdict(
    session: Any,
    graph: WorkflowGraphV3,
    request: AgentExecutionRequestV2,
) -> Verdict | None:
    """The verdict a finished execution of this node steers its loop with.

    Asked only where the document says a verdict decides something, so a run
    that steers nothing reads nothing. Where it does decide, the answer comes
    back from what that round kept rather than from anything recomputed: this is
    the recovery path, and the continuation it reports has to be the one the
    success write already took.
    """
    if verdict_condition_of(graph, request.node_id) is None:
        return None
    return read_verdict(load_kept_value(session, request.node_execution_id))


def _proof_of_a_passed_check(
    redemption: ToolRedemptionReceipt | None,
) -> ToolRedemptionReceipt | None:
    """This redemption where the project's check passed, and nothing where not.

    Every ending that keeps proof beside a failure asks here rather than reading
    the receipt itself, so no branch can persist a row saying the check did not
    pass. A nonzero exit is not a weaker proof of the same thing: it is the
    verdict that ends the attempt under `PROJECT_VERIFICATION_FAILED`, and a
    stored redemption is by definition the record of a command that was
    satisfied.
    """

    if redemption is None or not redemption.satisfied_the_project:
        return None
    return redemption


def _keep_tool_redemption(
    connection: Any,
    execution: AgentAttemptExecution,
    redemption: ToolRedemptionReceipt | None,
) -> None:
    """Keep what this attempt's grant redeemed, inside the write that succeeds it.

    A retry of the same durable attempt runs the verification again, and the row
    that is already there decides: identical evidence is the same redemption
    written twice, and different evidence is two answers about one attempt,
    which is a contradiction rather than a second receipt.

    Read back by the attempt, because the attempt is what this row is keyed by:
    a node execution can have a second attempt with a redemption of its own, and
    asking by node execution would find that one and call it a contradiction.
    """
    if redemption is None:
        return
    if not redemption.satisfied_the_project:
        raise ToolRedemptionConflict(
            "a stored tool redemption is the record of a check that passed"
        )
    if (
        redemption.node_execution_id != execution.request.node_execution_id
        or redemption.attempt_id != execution.attempt_id
    ):
        raise ToolRedemptionConflict("tool redemption differs from its exact attempt")
    connection.execute(
        tool_redemptions.insert()
        .prefix_with("OR IGNORE")
        .values(_tool_redemption_values(redemption))
    )
    stored = (
        connection.execute(
            sa.select(tool_redemptions).where(
                tool_redemptions.c.attempt_id == redemption.attempt_id.value
            )
        )
        .mappings()
        .one()
    )
    if _tool_redemption_from_record(stored) != redemption:
        raise ToolRedemptionConflict(
            "durable tool redemption differs from exact redemption"
        )


def _insert_attempt_event(
    connection: Any,
    attempt: AgentAttempt,
    kind: RunEventKind,
    *,
    command: AgentAttemptCancellation | None = None,
    replacement_attempt_id: AgentAttemptId | None = None,
) -> None:
    run = load_run(connection, attempt.run_id)
    sequence = run.last_event_sequence + 1
    payload = b"" if command is None else command.command_id.encode("utf-8")
    attempt_binding = (
        RunEventAgentAttemptBinding(attempt.attempt_id, attempt.attempt_ordinal)
        if command is None
        else RunEventCancellationBinding(
            attempt.attempt_id,
            attempt.attempt_ordinal,
            command.replacement,
            command.command_id,
            command.disposition,
            replacement_attempt_id,
        )
    )
    event = RunEvent(
        attempt.run_id,
        attempt.workflow_revision_hash,
        sequence,
        attempt.node_id,
        attempt.node_execution_id,
        kind,
        payload,
        attempt_binding=attempt_binding,
    )
    updated = connection.execute(
        runs.update()
        .where(
            runs.c.run_id == attempt.run_id.value,
            runs.c.revision_hash == attempt.workflow_revision_hash.value,
            runs.c.current_node_id == attempt.node_id,
            runs.c.state == RunState.STARTED.value,
            runs.c.state_version == run.state_version,
            runs.c.last_event_sequence == run.last_event_sequence,
        )
        .values(
            state_version=run.state_version + 1,
            last_event_sequence=sequence,
        )
    )
    if updated.rowcount != 1:
        raise RunTransitionConflict("agent attempt event lost the run-head CAS")
    _insert_event(connection, event)


def _lift_run_under_operator_cancel(connection: Any, terminal: AgentAttempt) -> None:
    """Close the run under its own operator cancel, in the same TX as its receipt.

    `CANCELLED` and `INTERRUPTED` are the two words a cleanup attestation can
    leave an attempt in, and #439 Bauplan P3 lifts the run the same way under
    both: the run's own ending follows the *command's* identity, not which of
    the two the disposition happened to be (the two-axis doctrine -- the node
    says how its own work ended, the run says why it stood still). A
    driver-loss or unavailable-executor cleanup reaches these same attempt
    words through a command of its own, never the operator's --
    `is_operator_run_cancel` is what tells the two apart, so neither of those
    two other callers of this cleanup path is touched here, and their runs
    stay exactly as unlifted and receipt-less as before this method existed.

    A replacement is never in flight here: `request_run_cancellation` always
    submits `AgentAttemptReplacement.NONE` (D2, #439 Bauplan P2/P3), so an
    operator command reaching this point never shares its node execution with
    a second attempt.
    """
    cancellation = terminal.cancellation
    if (
        cancellation is None
        or not is_operator_run_cancel(cancellation.command_id)
        or cancellation.replacement is not AgentAttemptReplacement.NONE
        or terminal.state
        not in {AgentAttemptState.CANCELLED, AgentAttemptState.INTERRUPTED}
    ):
        return
    run = load_run(connection, terminal.run_id)
    if run.state is not RunState.STARTED:
        return
    live_node_execution_id = NodeExecutionId.for_node(
        run.run_id, run.revision_hash, run.current_node_id, run.current_round_ordinal
    )
    if live_node_execution_id != terminal.node_execution_id:
        return
    if cancellation.disposition is None:
        raise RunTransitionConflict(
            "a terminal cancellation lifts its run only with an attested disposition"
        )
    keep_node_receipt(
        connection,
        terminal.node_execution_id,
        PersistedReceiptDisposition.CANCELLED,
        node_receipt_reason(
            NodeReceiptReason.CANCELLED_BY_OPERATOR, cancellation.disposition.value
        ),
    )
    lift_started_run(
        connection,
        run.run_id,
        run.revision_hash,
        run.state_version,
        run.last_event_sequence,
        RunState.CANCELLED,
    )


def _run_cancellation_from_event_log(
    connection: Any, run_id: RunId, command_id: str
) -> RunCancellationResult | None:
    """One accepted run-cancel command's canonical answer once its attempt
    row has moved on.

    Only a runner-carried success ever clears an attempt's cancellation
    columns (`_commit_success`), and it always does so in the very
    transaction that also writes that attempt's `AGENT_COMPLETED` event -- so
    by the time this command's row is gone, the event that decided its fate
    is already durable too. `None` means this exact command was never
    accepted at all: the caller is free to treat it as genuinely new.
    """
    requested = (
        connection.execute(
            sa.select(run_events.c.agent_attempt_id, run_events.c.event_sequence).where(
                run_events.c.run_id == run_id.value,
                run_events.c.cancellation_command_id == command_id,
                run_events.c.event_kind == RunEventKind.AGENT_CANCEL_REQUESTED.value,
            )
        )
        .mappings()
        .one_or_none()
    )
    if requested is None:
        return None
    terminal_kind = connection.scalar(
        sa.select(run_events.c.event_kind)
        .where(
            run_events.c.run_id == run_id.value,
            run_events.c.agent_attempt_id == requested["agent_attempt_id"],
            run_events.c.event_sequence > requested["event_sequence"],
            run_events.c.event_kind.in_(
                (
                    RunEventKind.AGENT_CANCELLED.value,
                    RunEventKind.AGENT_INTERRUPTED.value,
                    RunEventKind.AGENT_COMPLETED.value,
                )
            ),
        )
        .order_by(run_events.c.event_sequence)
        .limit(1)
    )
    run = load_run(connection, run_id)
    if terminal_kind == RunEventKind.AGENT_COMPLETED.value:
        return RunCancellationOvertakenBySuccess(run)
    if terminal_kind in (
        RunEventKind.AGENT_CANCELLED.value,
        RunEventKind.AGENT_INTERRUPTED.value,
    ):
        return RunCancellationTerminalRetry(run)
    return DurableStateCorrupt()


def _wait_cancellation_from_event_log(
    connection: Any, run_id: RunId, command_id: str
) -> RunCancellationResult | None:
    """This command's answer when it already ended a run resting at a pause.

    A resting Wait holds no attempt row, so the command id lives in the one
    place a wait cancellation writes it: the payload of its own attestation.
    That event is never rewritten, so a retry after a lost response reads the
    same answer forever. `None` means no wait cancellation carries this command.
    """
    ended = connection.scalar(
        sa.select(run_events.c.event_sequence).where(
            run_events.c.run_id == run_id.value,
            run_events.c.event_kind == RunEventKind.WAIT_CANCELLED.value,
            run_events.c.payload == command_id.encode("utf-8"),
        )
    )
    if ended is None:
        return None
    return RunCancellationEndedRun(load_run(connection, run_id))


def _cancel_resting_wait(
    connection: Any,
    run: RunV3,
    node_execution_id: NodeExecutionId,
    command_id: str,
) -> RunCancellationResult:
    """End a run resting at a pause, in the transaction that resolved it.

    Nothing is enqueued and nothing converges later: a pause has no attempt to
    stop, so the command writes its own attestation and the run is over when
    this returns. That is why the answer is `EndedRun` rather than `Accepted` --
    there is no cleanup an operator could still be waiting on.

    A pending answer is the one thing that refuses. The product has already told
    a person their message was taken, and applying it is a separate transaction
    away, so ending the run here would drop it silently; the operator is told to
    retry once that message has landed.
    """
    pending_answer = connection.scalar(
        sa.select(wait_answers.c.node_execution_id).where(
            wait_answers.c.node_execution_id == node_execution_id.value,
            wait_answers.c.state == WaitAnswerState.PENDING.value,
        )
    )
    if pending_answer is not None:
        return RunCancellationNotCancellable(RunCancellationRefusal.ANSWER_IN_FLIGHT)
    commit_wait_cancelled(
        connection,
        run.run_id,
        run.revision_hash,
        run.current_node_id,
        command_id,
        run.current_round_ordinal,
    )
    return RunCancellationEndedRun(load_run(connection, run.run_id))


def _unavailable_executor_cleanup_command_id(attempt_id: AgentAttemptId) -> str:
    return (
        f"{AgentExecutionRefusal.EXECUTOR_BINDING_UNAVAILABLE.value}:{attempt_id.value}"
    )


def _unavailable_executor_cleanup_request(
    attempt: AgentAttempt,
) -> CancelAgentAttemptRequest:
    cancellation = attempt.cancellation
    if cancellation is not None:
        return CancelAgentAttemptRequest(
            attempt.run_id,
            attempt.attempt_id,
            cancellation.command_id,
            cancellation.expected_attempt_state_version,
            cancellation.replacement,
        )
    return CancelAgentAttemptRequest(
        attempt.run_id,
        attempt.attempt_id,
        _unavailable_executor_cleanup_command_id(attempt.attempt_id),
        attempt.state_version,
        AgentAttemptReplacement.NONE,
    )


def _is_unavailable_executor_cleanup(attempt: AgentAttempt) -> bool:
    cancellation = attempt.cancellation
    return (
        attempt.attempt_ordinal == 1
        and attempt.runner_manifest_id is None
        and cancellation is not None
        and cancellation.command_id
        == _unavailable_executor_cleanup_command_id(attempt.attempt_id)
        and cancellation.replacement is AgentAttemptReplacement.NONE
    )


def _is_unavailable_executor_cleanup_complete(attempt: AgentAttempt) -> bool:
    cancellation = attempt.cancellation
    return (
        _is_unavailable_executor_cleanup(attempt)
        and attempt.state is AgentAttemptState.CANCELLED
        and attempt.process_phase is AgentAttemptProcessPhase.CLEANUP_ATTESTED
        and cancellation is not None
        and cancellation.disposition
        is AgentAttemptCancellationDisposition.NEVER_LAUNCHED
    )


def _commit_unavailable_executor_refusal(
    connection: Any, request: AgentExecutionRequestV2
) -> None:
    _commit_event(
        connection,
        request.run_id,
        request.workflow_revision_hash,
        request.node_id,
        RunEventKind.AGENT_FAILED,
        AgentExecutionRefusal.EXECUTOR_BINDING_UNAVAILABLE.value.encode("ascii"),
        RunState.STARTED,
        RunState.FAILED,
        request.node_id,
        terminal=True,
        round_ordinal=request.round_ordinal,
        target_round_ordinal=request.round_ordinal,
    )


def _unavailable_executor_refusal_is_already_terminal(
    connection: Any,
    request: AgentExecutionRequestV2,
    run: RunV2 | RunV3,
) -> bool:
    """Whether this exact pre-attempt terminal transition already committed."""

    if (
        run.state is not RunState.FAILED
        or run.current_node_id != request.node_id
        or run.current_round_ordinal != request.round_ordinal
    ):
        return False
    return (
        connection.execute(
            sa.select(run_events.c.event_sequence).where(
                run_events.c.run_id == request.run_id.value,
                run_events.c.revision_hash == request.workflow_revision_hash.value,
                run_events.c.node_id == request.node_id,
                run_events.c.node_execution_id == request.node_execution_id.value,
                run_events.c.event_kind == RunEventKind.AGENT_FAILED.value,
                run_events.c.payload
                == AgentExecutionRefusal.EXECUTOR_BINDING_UNAVAILABLE.value.encode(
                    "ascii"
                ),
                run_events.c.agent_attempt_id.is_(None),
                run_events.c.attempt_ordinal.is_(None),
                run_events.c.round_ordinal == request.round_ordinal,
            )
        ).one_or_none()
        is not None
    )


class DbosAgentAttemptStore:
    def __init__(self, engine: Engine, application_version: str | None = None) -> None:
        self._engine = engine
        self._application_version = application_version

    def prepare(self, execution: AgentAttemptExecution) -> AgentAttempt:
        request = execution.request
        prepared = _prepared_attempt(execution)
        with canonical_write_transaction(self._engine) as connection:
            run, _graph = _validate_request(
                connection, request, execution.attempt_id, execution.ordinal
            )
            if (
                run.state is not RunState.STARTED
                or run.current_node_id != request.node_id
                or execution.ordinal != 1
            ):
                existing = _load_attempt(connection, prepared.attempt_id)
                _require_attempt_binding(existing, execution)
                return existing
            inserted = connection.execute(
                agent_attempts.insert()
                .prefix_with("OR IGNORE")
                .values(_attempt_values(prepared))
            )
            if inserted.rowcount == 1:
                record_attempt_started(connection, prepared.attempt_id.value)
            durable = _load_attempt(connection, prepared.attempt_id)
            _require_attempt_binding(durable, execution)
            return durable

    def refuse_unavailable_executor(
        self, request: AgentExecutionRequestV2
    ) -> AgentExecutorBindingRefusalResult:
        """Close an unclaimed Agent node without inventing an attempt failure.

        The only mutable predecessor is ordinal one in PREPARED, which has not
        crossed the launch boundary. It first returns its existing cancellation
        cleanup request; callers carry that through the normal supervisor and
        workspace path, then retry this method. The same command, once accepted,
        stays on that cleanup path until NEVER_LAUNCHED is attested. Every armed,
        runner-bound, or foreign cancellation-in-progress record is fenced for
        #15.
        """

        attempt_id = AgentAttemptId.for_execution(
            request.node_execution_id, request.request_hash, 1
        )
        with canonical_write_transaction(self._engine) as connection:
            run, _graph = _validate_request(connection, request, attempt_id, 1)
            if _unavailable_executor_refusal_is_already_terminal(
                connection, request, run
            ):
                return AgentExecutorBindingRefusalWritten()
            record = (
                connection.execute(
                    sa.select(agent_attempts).where(
                        agent_attempts.c.attempt_id == attempt_id.value
                    )
                )
                .mappings()
                .one_or_none()
            )
            if record is None:
                _commit_unavailable_executor_refusal(connection, request)
                return AgentExecutorBindingRefusalWritten()
            attempt = attempt_from_record(record)
            if (
                attempt.node_execution_id != request.node_execution_id
                or attempt.request_hash != request.request_hash
                or attempt.run_id != request.run_id
                or attempt.workflow_revision_hash != request.workflow_revision_hash
                or attempt.node_id != request.node_id
                or attempt.attempt_ordinal != 1
            ):
                raise RunTransitionConflict(
                    "unavailable executor differs from durable attempt binding"
                )
            if (
                attempt.state is AgentAttemptState.PREPARED
                and attempt.runner_manifest_id is None
            ) or (
                _is_unavailable_executor_cleanup(attempt)
                and not _is_unavailable_executor_cleanup_complete(attempt)
            ):
                return AgentExecutorBindingRefusalNeedsPreparedCleanup(
                    attempt, _unavailable_executor_cleanup_request(attempt)
                )
            if _is_unavailable_executor_cleanup_complete(attempt):
                _commit_unavailable_executor_refusal(connection, request)
                return AgentExecutorBindingRefusalWritten()
            return AgentExecutorBindingRefusalFenced(attempt)

    def bind_runner_generation(
        self, execution: AgentAttemptExecution, binding: RunnerGenerationBinding
    ) -> AgentAttempt:
        if (
            binding.attempt_id != execution.attempt_id
            or binding.request_hash != execution.request.request_hash
        ):
            raise RunnerBindingConflict(
                "runner generation differs from the exact attempt request"
            )
        with canonical_write_transaction(self._engine) as connection:
            _validate_request(
                connection,
                execution.request,
                execution.attempt_id,
                execution.ordinal,
            )
            durable = _load_attempt(connection, execution.attempt_id)
            _require_attempt_binding(durable, execution)
            if durable.runner_manifest_id is not None:
                if (
                    durable.runner_manifest_id != binding.manifest_id
                    or durable.runner_generation_id != binding.generation_id
                ):
                    raise RunnerBindingConflict(
                        "runner binding retry differs from durable generation"
                    )
                return durable
            if (
                durable.state is not AgentAttemptState.PREPARED
                or durable.process_phase is not AgentAttemptProcessPhase.NONE
                or durable.runner_evidence_acceptance_phase
                is not RunnerEvidenceAcceptancePhase.NONE
            ):
                raise RunnerBindingConflict(
                    "only an unbound prepared attempt can bind a runner generation"
                )
            updated = connection.execute(
                agent_attempts.update()
                .where(
                    agent_attempts.c.attempt_id == durable.attempt_id.value,
                    agent_attempts.c.state == AgentAttemptState.PREPARED.value,
                    agent_attempts.c.state_version == durable.state_version,
                    agent_attempts.c.runner_manifest_id.is_(None),
                )
                .values(
                    state_version=durable.state_version + 1,
                    runner_manifest_id=binding.manifest_id.value,
                    runner_generation_id=binding.generation_id.value,
                )
            )
            if updated.rowcount != 1:
                raise RunnerBindingConflict("runner binding lost its attempt CAS")
            return _load_attempt(connection, durable.attempt_id)

    def arm_runner_invocation(
        self,
        execution: AgentAttemptExecution,
        binding: RunnerGenerationBinding,
        invocation_id: RunnerInvocationId,
    ) -> AgentAttempt:
        if (
            binding.attempt_id != execution.attempt_id
            or binding.request_hash != execution.request.request_hash
        ):
            raise RunnerBindingConflict(
                "runner invocation differs from the exact attempt request"
            )
        with canonical_write_transaction(self._engine) as connection:
            _validate_request(
                connection,
                execution.request,
                execution.attempt_id,
                execution.ordinal,
            )
            durable = _load_attempt(connection, execution.attempt_id)
            _require_attempt_binding(durable, execution)
            self._require_durable_runner_binding(durable, binding)
            if durable.runner_invocation_id is not None:
                if durable.runner_invocation_id != invocation_id:
                    raise RunnerBindingConflict(
                        "runner invocation retry differs from durable invocation"
                    )
                if durable.state is AgentAttemptState.PREPARED:
                    raise RunnerBindingConflict(
                        "prepared runner evidence did not arm its invocation"
                    )
                return durable
            if (
                durable.state is not AgentAttemptState.PREPARED
                or durable.runner_evidence_acceptance_phase
                is not RunnerEvidenceAcceptancePhase.NONE
            ):
                raise RunnerBindingConflict(
                    "only a bound prepared attempt can arm a runner invocation"
                )
            updated = connection.execute(
                agent_attempts.update()
                .where(
                    agent_attempts.c.attempt_id == durable.attempt_id.value,
                    agent_attempts.c.state == AgentAttemptState.PREPARED.value,
                    agent_attempts.c.state_version == durable.state_version,
                    agent_attempts.c.runner_invocation_id.is_(None),
                )
                .values(
                    state=AgentAttemptState.LAUNCH_ARMED.value,
                    state_version=durable.state_version + 1,
                    runner_invocation_id=invocation_id.value,
                )
            )
            if updated.rowcount != 1:
                raise RunnerBindingConflict("runner invocation lost its attempt CAS")
            return _load_attempt(connection, durable.attempt_id)

    @staticmethod
    def _require_durable_runner_binding(
        durable: AgentAttempt, binding: RunnerGenerationBinding
    ) -> None:
        if (
            durable.attempt_id != binding.attempt_id
            or durable.request_hash != binding.request_hash
            or durable.runner_manifest_id != binding.manifest_id
            or durable.runner_generation_id != binding.generation_id
        ):
            raise RunnerBindingConflict(
                "runner evidence differs from the durable generation binding"
            )

    @classmethod
    def _require_durable_runner_envelope(
        cls, durable: AgentAttempt, envelope: RunnerTerminalEvidenceEnvelope
    ) -> None:
        cls._require_durable_runner_binding(durable, envelope.binding)
        if durable.runner_invocation_id != envelope.invocation_id:
            raise RunnerBindingConflict(
                "runner evidence differs from the durable invocation"
            )

    @classmethod
    def _require_durable_runner_tombstone(
        cls, durable: AgentAttempt, tombstone: RunnerTerminalEvidenceAckTombstone
    ) -> None:
        cls._require_durable_runner_binding(durable, tombstone.binding)
        if durable.runner_invocation_id != tombstone.invocation_id:
            raise RunnerBindingConflict(
                "runner ACK differs from the durable invocation"
            )

    def bind_watchdog(
        self,
        execution: AgentAttemptExecution,
        process_owner_id: AgentProcessOwnerId,
        watchdog_generation_id: WatchdogGenerationId,
    ) -> AgentAttempt:
        with canonical_write_transaction(self._engine) as connection:
            _validate_request(
                connection,
                execution.request,
                execution.attempt_id,
                execution.ordinal,
            )
            durable = _load_attempt(connection, execution.attempt_id)
            _require_attempt_binding(durable, execution)
            if durable.process_phase is AgentAttemptProcessPhase.WATCHDOG_READY:
                if (
                    durable.process_owner_id != process_owner_id
                    or durable.watchdog_generation_id != watchdog_generation_id
                ):
                    raise RunTransitionConflict(
                        "watchdog retry differs from durable generation"
                    )
                return durable
            if (
                durable.state is not AgentAttemptState.PREPARED
                or durable.process_phase is not AgentAttemptProcessPhase.NONE
                or durable.runner_manifest_id is not None
            ):
                raise RunTransitionConflict(
                    "only a legacy unbound prepared attempt can bind a watchdog"
                )
            updated = connection.execute(
                agent_attempts.update()
                .where(
                    agent_attempts.c.attempt_id == durable.attempt_id.value,
                    agent_attempts.c.state == AgentAttemptState.PREPARED.value,
                    agent_attempts.c.state_version == durable.state_version,
                    agent_attempts.c.process_phase
                    == AgentAttemptProcessPhase.NONE.value,
                )
                .values(
                    state_version=durable.state_version + 1,
                    process_phase=AgentAttemptProcessPhase.WATCHDOG_READY.value,
                    process_owner_id=process_owner_id.value,
                    watchdog_generation_id=watchdog_generation_id.value,
                )
            )
            if updated.rowcount != 1:
                raise RunTransitionConflict("watchdog binding lost its attempt CAS")
            return _load_attempt(connection, durable.attempt_id)

    def claim(self, execution: AgentAttemptExecution) -> AgentAttemptClaimResult:
        request = execution.request
        attempt_id = execution.attempt_id
        with canonical_write_transaction(self._engine) as connection:
            run, graph = _validate_request(
                connection, request, execution.attempt_id, execution.ordinal
            )
            durable = _load_attempt(connection, attempt_id)
            _require_attempt_binding(durable, execution)
            if durable.runner_manifest_id is not None:
                raise RunTransitionConflict(
                    "a runner-bound attempt cannot enter the legacy claim path"
                )
            if durable.state is AgentAttemptState.PREPARED:
                if (
                    run.state is not RunState.STARTED
                    or run.current_node_id != request.node_id
                    or run.current_round_ordinal != request.round_ordinal
                ):
                    raise RunTransitionConflict(
                        "prepared attempt no longer owns current node"
                    )
                updated = connection.execute(
                    agent_attempts.update()
                    .where(
                        agent_attempts.c.attempt_id == attempt_id.value,
                        agent_attempts.c.state == AgentAttemptState.PREPARED.value,
                        agent_attempts.c.state_version == durable.state_version,
                    )
                    .values(
                        state=AgentAttemptState.LAUNCH_ARMED.value,
                        state_version=durable.state_version + 1,
                        process_phase=(
                            AgentAttemptProcessPhase.LAUNCH_AUTHORIZED.value
                            if durable.process_phase
                            is AgentAttemptProcessPhase.WATCHDOG_READY
                            else AgentAttemptProcessPhase.NONE.value
                        ),
                    )
                )
                durable = _load_attempt(connection, attempt_id)
                if updated.rowcount == 1:
                    return AgentAttemptClaimedByThisCall(durable)
            if durable.state is AgentAttemptState.LAUNCH_ARMED:
                return AgentAttemptPossiblyRan(durable)
            if durable.state is AgentAttemptState.FAILED:
                return AgentAttemptFailed(durable)
            if durable.state in {
                AgentAttemptState.CANCEL_REQUESTED,
                AgentAttemptState.CANCELLED,
                AgentAttemptState.INTERRUPTED,
            }:
                return AgentAttemptPossiblyRan(durable)
            if durable.state is AgentAttemptState.SUCCEEDED:
                completion = completion_after_node(
                    graph,
                    request.node_id,
                    request.round_ordinal,
                    _kept_verdict(connection, graph, request),
                )
                _require_completed_attempt_head(
                    connection,
                    run,
                    request,
                    completion,
                    _agent_platform_effect_completion_is_deferred(
                        connection, _agent_node_for_attempt(graph, request.node_id)
                    ),
                )
                return AgentAttemptSucceeded(durable, completion)
            raise AssertionError("closed agent attempt state was not exhaustive")

    def observe_process(
        self,
        execution: AgentAttemptExecution,
        process_owner_id: AgentProcessOwnerId,
        watchdog_generation_id: WatchdogGenerationId,
    ) -> AgentAttempt:
        with canonical_write_transaction(self._engine) as connection:
            _validate_request(
                connection,
                execution.request,
                execution.attempt_id,
                execution.ordinal,
            )
            durable = _load_attempt(connection, execution.attempt_id)
            _require_attempt_binding(durable, execution)
            if durable.process_phase is AgentAttemptProcessPhase.PROCESS_OBSERVED:
                if (
                    durable.process_owner_id != process_owner_id
                    or durable.watchdog_generation_id != watchdog_generation_id
                ):
                    raise RunTransitionConflict(
                        "observed process retry differs from durable generation"
                    )
                return durable
            updated = connection.execute(
                agent_attempts.update()
                .where(
                    agent_attempts.c.attempt_id == durable.attempt_id.value,
                    agent_attempts.c.state == AgentAttemptState.LAUNCH_ARMED.value,
                    agent_attempts.c.state_version == durable.state_version,
                    agent_attempts.c.process_phase
                    == AgentAttemptProcessPhase.LAUNCH_AUTHORIZED.value,
                    agent_attempts.c.process_owner_id == process_owner_id.value,
                    agent_attempts.c.watchdog_generation_id
                    == watchdog_generation_id.value,
                )
                .values(
                    state_version=durable.state_version + 1,
                    process_phase=AgentAttemptProcessPhase.PROCESS_OBSERVED.value,
                )
            )
            if updated.rowcount != 1:
                raise RunTransitionConflict("process observation lost its attempt CAS")
            return _load_attempt(connection, durable.attempt_id)

    def load(self, attempt_id: AgentAttemptId) -> AgentAttempt:
        with self._engine.connect() as connection:
            return _load_attempt(connection, attempt_id)

    def iter_driverless_attempts(self, page_limit: PageLimit) -> Iterator[AgentAttempt]:
        """Ask only after the durable runtime is launched.

        Before the launch, the workflow table this reads is either absent or
        still holds the statuses of the process that died, so every answer it
        could give would be about a machine that is not running yet. Which is
        also why the runtime's own application version is required rather than
        optional: a `PENDING` row a retired version left behind is never going to
        be recovered, and counting it as a live driver would hide this attempt
        from the sweep forever.
        """

        if self._application_version is None:
            raise RunTransitionConflict(
                "the driverless sweep requires the runtime application version"
            )
        terminal_states = tuple(state.value for state in TERMINAL_AGENT_ATTEMPT_STATES)
        after: AgentAttemptId | None = None
        while True:
            with self._engine.connect() as connection:
                query = sa.select(agent_attempts).where(
                    agent_attempts.c.state.not_in(terminal_states),
                    agent_attempts.c.runner_manifest_id.is_(None),
                )
                if after is not None:
                    query = query.where(agent_attempts.c.attempt_id > after.value)
                candidates = tuple(
                    attempt_from_record(record)
                    for record in connection.execute(
                        query.order_by(agent_attempts.c.attempt_id).limit(
                            page_limit.value
                        )
                    ).mappings()
                )
                if not candidates:
                    return
                drivers = tuple(
                    (attempt, driving_workflow_ids(attempt)) for attempt in candidates
                )
                driving = live_driver_workflow_ids(
                    connection,
                    (
                        workflow_id
                        for _attempt, workflow_ids in drivers
                        for workflow_id in workflow_ids
                    ),
                    self._application_version,
                )
            after = candidates[-1].attempt_id
            for attempt, workflow_ids in drivers:
                if driving.isdisjoint(workflow_ids):
                    yield attempt
            if len(candidates) < page_limit.value:
                return

    def commit_runner_terminal_evidence(
        self,
        execution: AgentAttemptExecution,
        envelope: RunnerTerminalEvidenceEnvelope,
    ) -> RunnerTerminalEvidenceCommitResult:
        if (
            envelope.binding.attempt_id != execution.attempt_id
            or envelope.binding.request_hash != execution.request.request_hash
        ):
            raise RunnerBindingConflict(
                "runner evidence differs from the exact attempt request"
            )
        evidence_hash = RunnerTerminalEvidenceHash.for_envelope(envelope)
        with canonical_write_transaction(self._engine) as connection:
            run, graph = _validate_request(
                connection,
                execution.request,
                execution.attempt_id,
                execution.ordinal,
            )
            durable = _load_attempt(connection, execution.attempt_id)
            _require_attempt_binding(durable, execution)
            if durable.runner_terminal_evidence_hash is not None:
                self._require_durable_runner_envelope(durable, envelope)
                if durable.runner_terminal_evidence_hash != evidence_hash:
                    raise RunnerBindingConflict(
                        "runner evidence retry differs from the durable evidence"
                    )
                completion = None
                if durable.state is AgentAttemptState.SUCCEEDED:
                    completion = completion_after_node(
                        graph,
                        execution.request.node_id,
                        execution.request.round_ordinal,
                        _kept_verdict(connection, graph, execution.request),
                    )
                return RunnerTerminalEvidenceCommitted(
                    durable, evidence_hash, completion
                )
            self._require_durable_runner_binding(durable, envelope.binding)
            node = graph.node(execution.request.node_id)
            if isinstance(node, AgentNodeV3) and node.tools:
                return RunnerTerminalEvidenceCommitRefused(
                    RunnerTerminalEvidenceRefusal.TOOL_GRANT_BOUND
                )
            evidence = envelope.evidence
            if isinstance(evidence, RunnerCancellation) and (
                evidence.observation is RunnerCancellationObservation.NEVER_LAUNCHED
            ):
                if (
                    durable.state is not AgentAttemptState.PREPARED
                    or durable.runner_invocation_id is not None
                ):
                    raise RunnerBindingConflict(
                        "never-launched evidence requires its bound pre-arm attempt"
                    )
                updated = connection.execute(
                    agent_attempts.update()
                    .where(
                        agent_attempts.c.attempt_id == durable.attempt_id.value,
                        agent_attempts.c.state == AgentAttemptState.PREPARED.value,
                        agent_attempts.c.state_version == durable.state_version,
                        agent_attempts.c.runner_terminal_evidence_hash.is_(None),
                    )
                    .values(
                        state_version=durable.state_version + 1,
                        runner_invocation_id=(
                            None
                            if envelope.invocation_id is None
                            else envelope.invocation_id.value
                        ),
                        runner_terminal_evidence_hash=evidence_hash.value,
                        runner_evidence_acceptance_phase=(
                            RunnerEvidenceAcceptancePhase.CORE_COMMITTED.value
                        ),
                    )
                )
                if updated.rowcount != 1:
                    raise RunnerBindingConflict(
                        "never-launched evidence lost its attempt CAS"
                    )
                return RunnerTerminalEvidenceCommitted(
                    _load_attempt(connection, durable.attempt_id), evidence_hash
                )

            self._require_durable_runner_envelope(durable, envelope)
            if isinstance(evidence, RunnerInvocationLost):
                if durable.state is not AgentAttemptState.LAUNCH_ARMED:
                    raise RunnerBindingConflict(
                        "invocation loss requires its armed attempt"
                    )
                updated = connection.execute(
                    agent_attempts.update()
                    .where(
                        agent_attempts.c.attempt_id == durable.attempt_id.value,
                        agent_attempts.c.state == AgentAttemptState.LAUNCH_ARMED.value,
                        agent_attempts.c.state_version == durable.state_version,
                        agent_attempts.c.runner_terminal_evidence_hash.is_(None),
                    )
                    .values(
                        state_version=durable.state_version + 1,
                        runner_terminal_evidence_hash=evidence_hash.value,
                        runner_evidence_acceptance_phase=(
                            RunnerEvidenceAcceptancePhase.CORE_COMMITTED.value
                        ),
                    )
                )
                if updated.rowcount != 1:
                    raise RunnerBindingConflict(
                        "invocation-loss evidence lost its attempt CAS"
                    )
                return RunnerTerminalEvidenceCommitted(
                    _load_attempt(connection, durable.attempt_id), evidence_hash
                )

            if (
                durable.state
                not in {
                    AgentAttemptState.LAUNCH_ARMED,
                    AgentAttemptState.CANCEL_REQUESTED,
                }
                or run.state is not RunState.STARTED
                or run.current_node_id != execution.request.node_id
                or run.current_round_ordinal != execution.request.round_ordinal
            ):
                raise RunnerBindingConflict(
                    "only the armed current runner attempt can accept terminal evidence"
                )
            if isinstance(evidence, RunnerProviderResult):
                outcome = self._commit_success(
                    connection,
                    execution,
                    durable,
                    run,
                    graph,
                    evidence.result,
                    runner_evidence_hash=evidence_hash,
                )
                return RunnerTerminalEvidenceCommitted(
                    outcome.attempt,
                    evidence_hash,
                    (
                        outcome.completion
                        if isinstance(outcome, AgentAttemptSucceeded)
                        else None
                    ),
                )
            if isinstance(evidence, RunnerProviderFailure):
                failed = _fail_current_attempt(
                    connection,
                    execution,
                    durable,
                    AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY,
                    node_receipt_reason(
                        NodeReceiptReason.PROCESS_EXITED_UNSUCCESSFULLY,
                        evidence.exit_signature.named(),
                    ),
                    runner_evidence_hash=evidence_hash,
                )
                return RunnerTerminalEvidenceCommitted(failed.attempt, evidence_hash)
            if isinstance(evidence, RunnerOutputLimitExceeded):
                streams = ", ".join(
                    stream.value
                    for stream in sorted(
                        evidence.exceeded_streams, key=lambda item: item.value
                    )
                )
                failed = _fail_current_attempt(
                    connection,
                    execution,
                    durable,
                    AgentAttemptFailureCode.PROCESS_OUTPUT_LIMIT_EXCEEDED,
                    node_receipt_reason(
                        NodeReceiptReason.PROCESS_OUTPUT_LIMIT_EXCEEDED, streams
                    ),
                    runner_evidence_hash=evidence_hash,
                )
                return RunnerTerminalEvidenceCommitted(failed.attempt, evidence_hash)
            if isinstance(evidence, RunnerProcessBoundaryFailure):
                failed = _fail_current_attempt(
                    connection,
                    execution,
                    durable,
                    AgentAttemptFailureCode.PROCESS_SUPERVISION_FAILED,
                    node_receipt_reason(NodeReceiptReason.PROCESS_SUPERVISION_FAILED),
                    runner_evidence_hash=evidence_hash,
                )
                return RunnerTerminalEvidenceCommitted(failed.attempt, evidence_hash)
            if isinstance(evidence, RunnerCancellation):
                return self._commit_runner_cancellation(
                    connection, durable, evidence, evidence_hash
                )
            raise AssertionError("closed Runner evidence union was not exhaustive")

    def _commit_success(
        self,
        connection: Any,
        execution: AgentAttemptExecution,
        durable: AgentAttempt,
        run: RunV2 | RunV3,
        graph: WorkflowGraphV3,
        result: AgentExecutionResult,
        *,
        redemption: ToolRedemptionReceipt | None = None,
        runner_evidence_hash: RunnerTerminalEvidenceHash | None = None,
    ) -> AgentAttemptSucceeded | AgentAttemptFailed:
        request = execution.request
        node = _agent_node_for_attempt(graph, request.node_id)
        declared = node.outputs[0]
        if declared.refusal is not None:
            named = agent_refusal_reason(result.output_bytes)
            if named is not None:
                failed = _fail_current_attempt(
                    connection,
                    execution,
                    durable,
                    AgentAttemptFailureCode.AGENT_REFUSED,
                    node_receipt_reason(NodeReceiptReason.AGENT_REFUSED, named),
                    AGENT_REFUSAL_SCHEMA.revision_hash,
                    result.output_bytes,
                    runner_evidence_hash,
                    result.transcript,
                    _proof_of_a_passed_check(redemption),
                )
                return failed
        refusal = _declared_output_schema_refusal(
            connection, node.id, declared, result.output_bytes
        )
        if refusal is not None:
            reason = _compact_schema_refusal(refusal, result.output_bytes)
            receipt_reason = node_receipt_reason(
                NodeReceiptReason.OUTPUT_SCHEMA_REFUSED, reason
            )
            refusal_receipt = _store_output_schema_refusal_receipt(
                connection,
                durable.attempt_id,
                receipt_reason,
                PublishedRevisionHash(declared.schema_reference.revision),
                result.output_bytes,
            )
            if execution.ordinal == 1:
                return self._begin_output_schema_repair(
                    connection,
                    execution,
                    durable,
                    graph,
                    node,
                    result,
                    refusal_receipt,
                    redemption,
                    runner_evidence_hash,
                )
            failed = _fail_current_attempt(
                connection,
                execution,
                durable,
                AgentAttemptFailureCode.OUTPUT_SCHEMA_REFUSED,
                receipt_reason,
                PublishedRevisionHash(declared.schema_reference.revision),
                result.output_bytes,
                runner_evidence_hash,
                result.transcript,
                _proof_of_a_passed_check(redemption),
            )
            return failed
        if redemption is not None and redemption.exit_code != 0:
            return _fail_current_attempt(
                connection,
                execution,
                durable,
                AgentAttemptFailureCode.PROJECT_VERIFICATION_FAILED,
                node_receipt_reason(
                    NodeReceiptReason.PROJECT_VERIFICATION_FAILED,
                    f"exit {redemption.exit_code}",
                ),
                runner_evidence_hash=runner_evidence_hash,
                transcript=result.transcript,
            )
        receipt = AgentReceiptV2.for_execution(request, run.binding_set_hash, result)
        connection.execute(
            agent_receipts_v2.insert()
            .prefix_with("OR IGNORE")
            .values(_agent_receipt_v2_values(receipt))
        )
        receipt_record = (
            connection.execute(
                sa.select(agent_receipts_v2).where(
                    agent_receipts_v2.c.node_execution_id
                    == request.node_execution_id.value
                )
            )
            .mappings()
            .one()
        )
        if _agent_receipt_v2_from_record(receipt_record) != receipt:
            raise AgentReceiptConflict(
                "durable V2 agent receipt differs from exact result"
            )
        _keep_tool_redemption(connection, execution, redemption)
        keep_node_receipt(
            connection,
            request.node_execution_id,
            PersistedReceiptDisposition.SUCCEEDED,
            node_receipt_reason(NodeReceiptReason.OUTPUT_ACCEPTED),
            NodeArtifact(
                request.run_id,
                node.id,
                request.node_execution_id,
                declared.name,
                PublishedRevisionHash(declared.schema_reference.revision),
                result.output_bytes,
            ),
        )
        values: dict[str, object] = {
            "state": AgentAttemptState.SUCCEEDED.value,
            "state_version": durable.state_version + 1,
            "receipt_hash": receipt.receipt_hash.value,
            **_kept_transcript_values(connection, result.transcript),
        }
        if runner_evidence_hash is not None:
            values.update(
                runner_terminal_evidence_hash=runner_evidence_hash.value,
                runner_evidence_acceptance_phase=(
                    RunnerEvidenceAcceptancePhase.CORE_COMMITTED.value
                ),
            )
            if durable.state is AgentAttemptState.CANCEL_REQUESTED:
                values.update(
                    cancellation_command_id=None,
                    cancellation_expected_state_version=None,
                    replacement=None,
                    redrive_state=None,
                    cancellation_disposition=None,
                    cancellation_workflow_id=None,
                )
        updated = connection.execute(
            agent_attempts.update()
            .where(
                agent_attempts.c.attempt_id == durable.attempt_id.value,
                agent_attempts.c.state == durable.state.value,
                agent_attempts.c.state_version == durable.state_version,
            )
            .values(**values)
        )
        if updated.rowcount != 1:
            raise RunTransitionConflict("agent success lost its attempt CAS")
        record_attempt_ended(connection, durable.attempt_id.value)
        completion = completion_after_node(
            graph,
            request.node_id,
            request.round_ordinal,
            None
            if verdict_condition_of(graph, request.node_id) is None
            else read_verdict(result.output_bytes),
        )
        if _agent_platform_effect_completion_is_deferred(connection, node):
            target_state = RunState.STARTED
            target_node_id = request.node_id
            target_round_ordinal = request.round_ordinal
            terminal = False
        else:
            match completion:
                case RunContinues(node_id, target_round):
                    target_state = RunState.STARTED
                    target_node_id = node_id
                    target_round_ordinal = target_round
                    terminal = False
                case RunCompletes():
                    target_state = RunState.COMPLETED
                    target_node_id = request.node_id
                    target_round_ordinal = request.round_ordinal
                    terminal = True
                case _ as unreachable:
                    assert_never(unreachable)
        _commit_event(
            connection,
            request.run_id,
            request.workflow_revision_hash,
            request.node_id,
            RunEventKind.AGENT_COMPLETED,
            result.output_bytes,
            RunState.STARTED,
            target_state,
            target_node_id,
            terminal=terminal,
            agent_attempt_id=durable.attempt_id,
            attempt_ordinal=execution.ordinal,
            agent_receipt_hash=receipt.receipt_hash,
            round_ordinal=request.round_ordinal,
            target_round_ordinal=target_round_ordinal,
        )
        return AgentAttemptSucceeded(
            _load_attempt(connection, durable.attempt_id), completion
        )

    def _begin_output_schema_repair(
        self,
        connection: Any,
        execution: AgentAttemptExecution,
        durable: AgentAttempt,
        graph: WorkflowGraphV3,
        node: AgentNodeV3,
        result: AgentExecutionResult,
        refusal_receipt: OutputSchemaRefusalReceipt,
        redemption: ToolRedemptionReceipt | None,
        runner_evidence_hash: RunnerTerminalEvidenceHash | None,
    ) -> AgentAttemptFailed:
        request = execution.request
        failed = _fail_current_attempt(
            connection,
            execution,
            durable,
            AgentAttemptFailureCode.OUTPUT_SCHEMA_REFUSED,
            refusal_receipt.reason,
            refusal_receipt.schema_revision,
            result.output_bytes,
            runner_evidence_hash,
            result.transcript,
            _proof_of_a_passed_check(redemption),
            terminal_node_failure=False,
        )
        repair_request = _output_schema_repair_request(
            connection, request, graph, node, refusal_receipt
        )
        repair = _prepared_attempt(
            AgentAttemptExecution(
                repair_request,
                AgentAttemptId.for_execution(
                    repair_request.node_execution_id,
                    repair_request.request_hash,
                    REPLACEMENT_AGENT_ATTEMPT_ORDINAL,
                ),
                REPLACEMENT_AGENT_ATTEMPT_ORDINAL,
            )
        )
        connection.execute(agent_attempts.insert().values(_attempt_values(repair)))
        record_attempt_started(connection, repair.attempt_id.value)
        if self._application_version is None:
            raise RunTransitionConflict(
                "output-schema repair requires an application version"
            )
        client = DBOSClient(
            system_database_engine=self._engine, use_listen_notify=False
        )
        try:
            client.enqueue_in_transaction(
                connection,
                {
                    "workflow_name": REPLACEMENT_WORKFLOW_NAME,
                    "queue_name": QUEUE_NAME,
                    "workflow_id": replacement_workflow_id_for(repair.attempt_id),
                    "app_version": self._application_version,
                },
                repair.attempt_id.value,
            )
        finally:
            client.destroy()
        return failed

    def _commit_runner_cancellation(
        self,
        connection: Any,
        durable: AgentAttempt,
        evidence: RunnerCancellation,
        evidence_hash: RunnerTerminalEvidenceHash,
    ) -> RunnerTerminalEvidenceCommitted:
        cancellation = durable.cancellation
        if (
            durable.state is not AgentAttemptState.CANCEL_REQUESTED
            or cancellation is None
            or cancellation.command_id != evidence.command_id
            or cancellation.replacement is not AgentAttemptReplacement.NONE
        ):
            raise RunnerBindingConflict(
                "runner cancellation evidence differs from its Core command"
            )
        dispositions = {
            RunnerCancellationObservation.EXITED_BEFORE_SIGNAL: (
                AgentAttemptCancellationDisposition.EXITED_BEFORE_SIGNAL
            ),
            RunnerCancellationObservation.REAPED_AFTER_TERM: (
                AgentAttemptCancellationDisposition.REAPED_AFTER_TERM
            ),
            RunnerCancellationObservation.REAPED_AFTER_KILL: (
                AgentAttemptCancellationDisposition.REAPED_AFTER_KILL
            ),
        }
        disposition = dispositions.get(evidence.observation)
        if disposition is None:
            raise RunnerBindingConflict(
                "never-launched evidence cannot terminally cancel an attempt"
            )
        updated = connection.execute(
            agent_attempts.update()
            .where(
                agent_attempts.c.attempt_id == durable.attempt_id.value,
                agent_attempts.c.state == AgentAttemptState.CANCEL_REQUESTED.value,
                agent_attempts.c.state_version == durable.state_version,
                agent_attempts.c.cancellation_command_id == evidence.command_id,
                agent_attempts.c.runner_terminal_evidence_hash.is_(None),
            )
            .values(
                state=AgentAttemptState.CANCELLED.value,
                state_version=durable.state_version + 1,
                redrive_state=AgentAttemptRedriveState.CLEANUP_ATTESTED.value,
                cancellation_disposition=disposition.value,
                runner_terminal_evidence_hash=evidence_hash.value,
                runner_evidence_acceptance_phase=(
                    RunnerEvidenceAcceptancePhase.CORE_COMMITTED.value
                ),
            )
        )
        if updated.rowcount != 1:
            raise RunnerBindingConflict("runner cancellation lost its attempt CAS")
        record_attempt_ended(connection, durable.attempt_id.value)
        terminal = _load_attempt(connection, durable.attempt_id)
        _insert_attempt_event(
            connection,
            terminal,
            RunEventKind.AGENT_CANCELLED,
            command=terminal.cancellation,
        )
        _lift_run_under_operator_cancel(connection, terminal)
        return RunnerTerminalEvidenceCommitted(terminal, evidence_hash)

    def commit_never_launched_cancellation(
        self, request: CancelAgentAttemptRequest
    ) -> AgentAttemptCancellationAccepted:
        """End one leased-but-never-launched runner attempt under operator cancel.

        The only proof this transition ever runs on is a *won* lease withdraw,
        which its caller (`cancel_runner_attempt`) has already obtained; this
        write never re-derives it. The terminal row keeps the attempt's runner
        binding (`runner_manifest_id`/`runner_generation_id` preserved) so it
        stays legible as a runner attempt, leaves `runner_invocation_id` NULL
        and fabricates no evidence -- acceptance phase stays `NONE`, the
        terminal evidence hash stays NULL -- and records disposition
        `NEVER_LAUNCHED` on the redrive axis, with `process_phase` `NONE`
        because a runner-bound row may never carry `CLEANUP_ATTESTED`.

        Idempotent: a re-run after the commit already landed reads the durable
        terminal row and returns it without a second CAS, event or run lift,
        exactly like `attest_cancellation_cleanup`'s own terminal short-circuit.
        """

        with canonical_write_transaction(self._engine) as connection:
            attempt = _load_attempt(connection, request.attempt_id)
            if attempt.runner_manifest_id is None:
                raise RunTransitionConflict(
                    "never-launched cancellation requires a runner-bound attempt"
                )
            cancellation = attempt.cancellation
            if cancellation is None or not cancellation.matches(request):
                raise RunTransitionConflict(
                    "never-launched cancel differs from its cancellation command"
                )
            if attempt.state in {
                AgentAttemptState.CANCELLED,
                AgentAttemptState.INTERRUPTED,
            }:
                if (
                    cancellation.disposition
                    is not AgentAttemptCancellationDisposition.NEVER_LAUNCHED
                ):
                    raise RunTransitionConflict(
                        "never-launched cancel retry differs from durable disposition"
                    )
                return AgentAttemptCancellationAccepted(
                    attempt,
                    True,
                    self._replacement_attempt_id(connection, attempt),
                )
            if attempt.state is not AgentAttemptState.CANCEL_REQUESTED:
                raise RunTransitionConflict(
                    "only a requested cancellation can commit never-launched"
                )
            if attempt.runner_invocation_id is not None:
                raise RunTransitionConflict(
                    "a launched runner attempt cannot commit never-launched"
                )
            updated = connection.execute(
                agent_attempts.update()
                .where(
                    agent_attempts.c.attempt_id == attempt.attempt_id.value,
                    agent_attempts.c.state == AgentAttemptState.CANCEL_REQUESTED.value,
                    agent_attempts.c.state_version == attempt.state_version,
                    agent_attempts.c.cancellation_command_id == request.command_id,
                    agent_attempts.c.runner_invocation_id.is_(None),
                )
                .values(
                    state=AgentAttemptState.CANCELLED.value,
                    state_version=attempt.state_version + 1,
                    redrive_state=AgentAttemptRedriveState.CLEANUP_ATTESTED.value,
                    cancellation_disposition=(
                        AgentAttemptCancellationDisposition.NEVER_LAUNCHED.value
                    ),
                )
            )
            if updated.rowcount != 1:
                raise RunTransitionConflict(
                    "never-launched cancellation lost its attempt CAS"
                )
            record_attempt_ended(connection, attempt.attempt_id.value)
            terminal = _load_attempt(connection, attempt.attempt_id)
            _insert_attempt_event(
                connection,
                terminal,
                RunEventKind.AGENT_CANCELLED,
                command=terminal.cancellation,
            )
            _lift_run_under_operator_cancel(connection, terminal)
            return AgentAttemptCancellationAccepted(
                terminal,
                True,
                self._replacement_attempt_id(connection, terminal),
            )

    def mark_runner_evidence_acknowledged(
        self,
        execution: AgentAttemptExecution,
        tombstone: RunnerTerminalEvidenceAckTombstone,
    ) -> AgentAttempt:
        evidence_hash = tombstone.evidence_hash
        with canonical_write_transaction(self._engine) as connection:
            durable = _load_attempt(connection, execution.attempt_id)
            _require_attempt_binding(durable, execution)
            self._require_durable_runner_tombstone(durable, tombstone)
            if durable.runner_terminal_evidence_hash != evidence_hash:
                raise RunnerBindingConflict(
                    "runner ACK differs from the durable evidence"
                )
            if (
                durable.runner_evidence_acceptance_phase
                is RunnerEvidenceAcceptancePhase.ACKNOWLEDGED
            ):
                return durable
            if (
                durable.runner_evidence_acceptance_phase
                is not RunnerEvidenceAcceptancePhase.CORE_COMMITTED
            ):
                raise RunnerBindingConflict(
                    "runner evidence must commit before it can be acknowledged"
                )
            updated = connection.execute(
                agent_attempts.update()
                .where(
                    agent_attempts.c.attempt_id == durable.attempt_id.value,
                    agent_attempts.c.state_version == durable.state_version,
                    agent_attempts.c.runner_terminal_evidence_hash
                    == evidence_hash.value,
                    agent_attempts.c.runner_evidence_acceptance_phase
                    == RunnerEvidenceAcceptancePhase.CORE_COMMITTED.value,
                )
                .values(
                    state_version=durable.state_version + 1,
                    runner_evidence_acceptance_phase=(
                        RunnerEvidenceAcceptancePhase.ACKNOWLEDGED.value
                    ),
                )
            )
            if updated.rowcount != 1:
                raise RunnerBindingConflict("runner ACK lost its attempt CAS")
            return _load_attempt(connection, durable.attempt_id)

    def complete_success(
        self,
        execution: AgentAttemptExecution,
        result: AgentExecutionResult,
        redemption: ToolRedemptionReceipt | None = None,
    ) -> AgentAttemptSucceeded | AgentAttemptFailed:
        """Write the one success this attempt is allowed, or its named refusal.

        A V3 node's declared output is what its author promised, so the exact
        decoded bytes are read against the schema it pins before anything here is
        written. Bytes their own schema refuses leave no agent receipt, no
        `AGENT_COMPLETED` event and no advanced run -- a success nobody may take
        back must not be written for an answer this product cannot honour. What
        the refusal leaves instead is its durable name: an immutable Attempt
        receipt carrying the compact schema-refusal diagnosis. An ordinal-one
        refusal records a nonterminal `AGENT_FAILED` event and orders its repair;
        only an ordinal-two refusal also writes the terminal `failed`
        `node-receipt/v3`. Both attempts use the same failure seam
        `PROCESS_EXITED_UNSUCCESSFULLY` runs on today, so the driver ends named
        instead of dying on an exception nobody stored.
        A granted verification that exits nonzero is the same named seam under
        `PROJECT_VERIFICATION_FAILED`, with how the command ended in the reason
        and without a `tool_redemptions` row.

        A V3 success additionally keeps what the run now knows durably: the
        produced value as `node-artifact/v3` and the `succeeded`
        `node-receipt/v3` naming it, in this same transaction.
        """
        request = execution.request
        attempt_id = execution.attempt_id
        with canonical_write_transaction(self._engine) as connection:
            run, graph = _validate_request(
                connection, request, execution.attempt_id, execution.ordinal
            )
            durable = _load_attempt(connection, attempt_id)
            _require_attempt_binding(durable, execution)
            if (
                durable.state is not AgentAttemptState.LAUNCH_ARMED
                or run.state is not RunState.STARTED
                or run.current_node_id != request.node_id
                or run.current_round_ordinal != request.round_ordinal
            ):
                raise RunTransitionConflict(
                    "only the armed current attempt can succeed"
                )
            return self._commit_success(
                connection,
                execution,
                durable,
                run,
                graph,
                result,
                redemption=redemption,
            )

    def complete_known_failure(
        self,
        execution: AgentAttemptExecution,
        exit_signature: ProcessExitSignature,
        transcript: AttemptTranscript | None = None,
    ) -> AgentAttemptFailed:
        """End the attempt whose process left no answer, and say what it left.

        The exit signature is what the supervision saw from outside: how the
        child ended and the standard error it left. It reaches the `failed`
        `node-receipt/v3` on the same seam a refused answer uses, and no further
        -- the `AGENT_FAILED` event keeps carrying the bare code, so the stream
        stays the bounded surface a reader may subscribe to without reading a
        provider's own output.

        `transcript` is what the executor could read of what the process itself
        wrote, and it is kept beside that. An exit code and an empty standard
        error was the whole account of a real failed run (#733), which is to say
        no account at all; the steps the process got through, and whatever it
        printed instead of a stream, are the only place the reason can be.
        """
        request = execution.request
        with canonical_write_transaction(self._engine) as connection:
            run, _graph = _validate_request(
                connection, request, execution.attempt_id, execution.ordinal
            )
            durable = _load_attempt(connection, execution.attempt_id)
            _require_attempt_binding(durable, execution)
            if (
                durable.state is not AgentAttemptState.LAUNCH_ARMED
                or run.state is not RunState.STARTED
                or run.current_node_id != request.node_id
            ):
                raise RunTransitionConflict("only the armed current attempt can fail")
            return _fail_current_attempt(
                connection,
                execution,
                durable,
                AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY,
                node_receipt_reason(
                    NodeReceiptReason.PROCESS_EXITED_UNSUCCESSFULLY,
                    exit_signature.named(),
                ),
                transcript=transcript,
            )

    def complete_agent_refusal(
        self, execution: AgentAttemptExecution, reason: str
    ) -> AgentAttemptFailed:
        """End an armed attempt whose executor did not start a provider process."""

        return self._judged_armed_failure(
            execution,
            AgentAttemptFailureCode.AGENT_REFUSED,
            NodeReceiptReason.AGENT_REFUSED,
            reason,
            None,
        )

    def complete_project_verification_failure(
        self,
        execution: AgentAttemptExecution,
        verdict: str,
        transcript: AttemptTranscript | None = None,
    ) -> AgentAttemptFailed:
        """End the armed attempt whose granted verification never produced an exit.

        The project's command was started after this live call had already claimed;
        it then did not answer, so there is no exit code to keep and no
        `tool_redemptions` row. The attempt ends on the same
        `PROJECT_VERIFICATION_FAILED` seam a nonzero exit uses, with `verdict`
        naming why -- the declared timeout, not an invented code.

        The provider had already answered when the check went silent, so its
        steps are kept here too. Losing them on this one path would make the
        transcript's absence mean two different things -- "no executor decoded
        one" and "a verification timed out afterwards" -- and a reader could not
        tell which.
        """
        return self._judged_armed_failure(
            execution,
            AgentAttemptFailureCode.PROJECT_VERIFICATION_FAILED,
            NodeReceiptReason.PROJECT_VERIFICATION_FAILED,
            verdict,
            transcript,
        )

    def complete_candidate_capture_failure(
        self,
        execution: AgentAttemptExecution,
        verdict: str,
        transcript: AttemptTranscript | None = None,
        redemption: ToolRedemptionReceipt | None = None,
    ) -> AgentAttemptFailed:
        """End the armed attempt whose finished work could not be kept.

        Nothing about this attempt went wrong except the last thing: the process
        answered, the schema admitted the bytes and any granted check passed.
        What failed is the keeping, so `verdict` carries the store's own words
        rather than an exit code no process produced.

        `redemption` is that passed check's own proof, and it becomes durable in
        this same write. The check really ran and really exited zero; the work
        being unkeepable afterwards says nothing about it, and discarding its
        evidence would leave an operator unable to tell a project whose tests
        pass from one whose tests were never satisfied.

        The provider's steps are kept here for a stronger reason than anywhere
        else. Once the workspace is released, the transcript is the only
        surviving evidence that this work was ever done at all.
        """
        return self._judged_armed_failure(
            execution,
            AgentAttemptFailureCode.CANDIDATE_CAPTURE_FAILED,
            NodeReceiptReason.CANDIDATE_CAPTURE_FAILED,
            verdict,
            transcript,
            _proof_of_a_passed_check(redemption),
        )

    def _judged_armed_failure(
        self,
        execution: AgentAttemptExecution,
        failure: AgentAttemptFailureCode,
        token: NodeReceiptReason,
        verdict: str,
        transcript: AttemptTranscript | None,
        redemption: ToolRedemptionReceipt | None = None,
    ) -> AgentAttemptFailed:
        """End this run's armed current attempt under one judged ending.

        Every ending an attempt reaches *after* its claim has won -- and that
        no process exit judged -- passes through here, because which attempt may
        end is one rule: the armed current attempt of a started run, and nothing
        else. Two copies of that rule would be two chances for it to drift.
        """
        if not verdict:
            raise ValueError(f"an ending named {token.value} says why it happened")
        request = execution.request
        with canonical_write_transaction(self._engine) as connection:
            run, _graph = _validate_request(
                connection, request, execution.attempt_id, execution.ordinal
            )
            durable = _load_attempt(connection, execution.attempt_id)
            _require_attempt_binding(durable, execution)
            if (
                durable.state is not AgentAttemptState.LAUNCH_ARMED
                or run.state is not RunState.STARTED
                or run.current_node_id != request.node_id
            ):
                raise RunTransitionConflict("only the armed current attempt can fail")
            return _fail_current_attempt(
                connection,
                execution,
                durable,
                failure,
                node_receipt_reason(token, verdict),
                transcript=transcript,
                redemption=redemption,
            )

    def request_cancellation(
        self, request: CancelAgentAttemptRequest
    ) -> AgentAttemptCancellationResult:
        with canonical_write_transaction(self._engine) as connection:
            run_record = connection.scalar(
                sa.select(runs.c.run_id).where(runs.c.run_id == request.run_id.value)
            )
            if run_record is None:
                return AgentAttemptCancellationRunMissing()
            record = (
                connection.execute(
                    sa.select(agent_attempts).where(
                        agent_attempts.c.attempt_id == request.attempt_id.value
                    )
                )
                .mappings()
                .one_or_none()
            )
            if record is None:
                return AgentAttemptCancellationTargetMissing()
            attempt = attempt_from_record(record)
            if attempt.run_id != request.run_id:
                return AgentAttemptCancellationTargetMissing()
            existing = attempt.cancellation
            if existing is not None:
                if not existing.matches(request):
                    return AgentAttemptCancellationCommandConflict()
                return AgentAttemptCancellationAccepted(
                    attempt,
                    attempt.state
                    in {AgentAttemptState.CANCELLED, AgentAttemptState.INTERRUPTED},
                    self._replacement_attempt_id(connection, attempt),
                )
            if attempt.state in {
                AgentAttemptState.SUCCEEDED,
                AgentAttemptState.FAILED,
                AgentAttemptState.CANCELLED,
                AgentAttemptState.INTERRUPTED,
            }:
                return AgentAttemptCancellationTerminalConflict()
            if attempt.state_version != request.expected_attempt_state_version:
                return AgentAttemptCancellationStale()
            if (
                attempt.runner_manifest_id is not None
                and request.replacement is AgentAttemptReplacement.ONE
            ):
                return AgentAttemptReplacementNotAllowed()
            if request.replacement is AgentAttemptReplacement.ONE and (
                attempt.attempt_ordinal != 1
            ):
                return AgentAttemptReplacementNotAllowed()
            current_ordinal = connection.scalar(
                sa.select(sa.func.max(agent_attempts.c.attempt_ordinal)).where(
                    agent_attempts.c.node_execution_id
                    == attempt.node_execution_id.value
                )
            )
            run = load_run(connection, request.run_id)
            if (
                run.state is not RunState.STARTED
                or run.current_node_id != attempt.node_id
                or int(current_ordinal or 0) != attempt.attempt_ordinal
            ):
                return AgentAttemptCancellationNotCurrent()
            committed = self._commit_new_cancellation(connection, attempt, request)
            if committed is None:
                return AgentAttemptCancellationStale()
            return AgentAttemptCancellationAccepted(committed, False)

    def request_run_cancellation(
        self, request: CancelRunRequest
    ) -> RunCancellationResult:
        """Resolve one operator run-cancel command against the store's own truth.

        Ordering is load-bearing (#439 Bauplan P2), not incidental:

        1. **A known command answers first, before any state gate.** The
           attempt row carrying this exact command id -- whatever state it
           reached -- is the canonical answer regardless of where the run
           stands today; a retry after a lost response must never be told
           "not cancellable" merely because the run moved on since the
           command it already answered.
        2. **The success-wins fallback.** A runner-carried success clears that
           row's cancellation columns in the same transaction that writes
           `AGENT_COMPLETED` (`_commit_success`), so the row this command
           stamped can vanish out from under it. The command's own
           `AGENT_CANCEL_REQUESTED` event survives that clearing -- events are
           never rewritten -- so a retry that misses the row reads the event
           log instead and answers from whichever terminal event followed.
        3. **Only a genuinely new command reaches the cancellability gate:**
           the run must be `STARTED`, and the node execution the operator's
           confirmation named is recomputed from durable truth rather than
           trusted, exactly like every other execution identity a CAS
           transaction in this module already recomputes (D2, #439 Bauplan).
        4. **The write itself is `request_cancellation`'s own CAS body** --
           `_commit_new_cancellation` is the one place both make it.
        """
        command_id = RunCancelCommandId.for_key(request.idempotency_key).value
        with canonical_write_transaction(self._engine) as connection:
            record = (
                connection.execute(
                    sa.select(agent_attempts).where(
                        agent_attempts.c.cancellation_command_id == command_id,
                        agent_attempts.c.run_id == request.run_id.value,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if record is not None:
                attempt = attempt_from_record(record)
                if attempt.state is AgentAttemptState.CANCEL_REQUESTED:
                    return RunCancellationAccepted(attempt)
                if attempt.state in {
                    AgentAttemptState.CANCELLED,
                    AgentAttemptState.INTERRUPTED,
                }:
                    return RunCancellationTerminalRetry(
                        load_run(connection, request.run_id)
                    )
                return DurableStateCorrupt()

            from_event_log = _run_cancellation_from_event_log(
                connection, request.run_id, command_id
            ) or _wait_cancellation_from_event_log(
                connection, request.run_id, command_id
            )
            if from_event_log is not None:
                return from_event_log

            run_record = connection.scalar(
                sa.select(runs.c.run_id).where(runs.c.run_id == request.run_id.value)
            )
            if run_record is None:
                return RunCancellationRunMissing()
            run = load_run(connection, request.run_id)
            if run.state in TERMINAL_RUN_STATES:
                return RunCancellationNotCancellable(
                    RunCancellationRefusal.ALREADY_ENDED
                )
            resting_wait_run = (
                run
                if run.state is RunState.WAITING_INPUT and isinstance(run, RunV3)
                else None
            )
            waiting_for_a_person = run.state in {
                RunState.WAITING_INPUT,
                RunState.WAITING_RECONCILIATION,
            }
            # A reconciliation pause keeps `waiting-for-you`: an Action's live
            # intent stands behind it, and ending the run there would abandon
            # it. So does any pause a format-3 line did not write, because
            # `WAIT_CANCELLED` is a kind only the V3 wire publishes (#668).
            if waiting_for_a_person and resting_wait_run is None:
                return RunCancellationNotCancellable(
                    RunCancellationRefusal.WAITING_FOR_YOU
                )

            live_node_execution_id = NodeExecutionId.for_node(
                run.run_id,
                run.revision_hash,
                run.current_node_id,
                run.current_round_ordinal,
            )
            if live_node_execution_id != request.expected_node_execution_id:
                return RunCancellationNotCancellable(
                    RunCancellationRefusal.BETWEEN_NODES
                )

            if resting_wait_run is not None:
                return _cancel_resting_wait(
                    connection, resting_wait_run, live_node_execution_id, command_id
                )

            graph = load_graph(connection, run.revision_hash)
            current_node = graph.node(run.current_node_id)
            if not isinstance(current_node, AgentNodeV3):
                return RunCancellationNotCancellable(
                    RunCancellationRefusal.NODE_RUNS_NO_AGENT
                )

            current_record = (
                connection.execute(
                    sa.select(agent_attempts)
                    .where(
                        agent_attempts.c.node_execution_id
                        == live_node_execution_id.value
                    )
                    .order_by(agent_attempts.c.attempt_ordinal.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
            if current_record is None:
                return RunCancellationNotCancellable(
                    RunCancellationRefusal.BETWEEN_NODES
                )
            current_attempt = attempt_from_record(current_record)
            if current_attempt.state in TERMINAL_AGENT_ATTEMPT_STATES:
                # The run is still STARTED -- only its current node's attempt
                # ended, so `already-ended` would name the run dead when it is
                # about to move on. That is the exact standing the projection
                # shows the operator as `between-nodes`; both paths speak one
                # honest sentence for one durable state (#439 P6).
                return RunCancellationNotCancellable(
                    RunCancellationRefusal.BETWEEN_NODES
                )
            if current_attempt.cancellation is not None:
                # Some other command -- the attempt route, or an earlier
                # idempotency key -- already owns this attempt's cancellation.
                return RunCancellationCommandConflict()

            cancel_request = CancelAgentAttemptRequest(
                request.run_id,
                current_attempt.attempt_id,
                command_id,
                current_attempt.state_version,
                AgentAttemptReplacement.NONE,
            )
            committed = self._commit_new_cancellation(
                connection, current_attempt, cancel_request
            )
            if committed is None:
                return RunCancellationCommandConflict()
            return RunCancellationAccepted(committed)

    def _commit_new_cancellation(
        self,
        connection: Any,
        attempt: AgentAttempt,
        request: CancelAgentAttemptRequest,
    ) -> AgentAttempt | None:
        """Stamp `CANCEL_REQUESTED` under one command and enqueue its cleanup.

        The CAS body `request_cancellation` and `request_run_cancellation`
        share: both resolve which non-terminal attempt, at which version, a
        genuinely new command targets before calling this, so from here the
        write is the same either way. `None` means the CAS lost its race --
        the two callers name that in their own, different vocabularies
        (`Stale` for a client-supplied version; `CommandConflict` for a
        server-resolved one), so the naming stays here.

        Every accepted command -- local-process and runner-lease alike --
        enqueues the one carrier-aware cancellation workflow in this same
        transaction. A runner-lease-bound attempt used to return here with
        nothing enqueued, which left an operator's run-cancel stamped
        `CANCEL_REQUESTED` with no owner to converge it (#584); the workflow
        itself now dispatches on the durable carrier.
        """
        workflow_id = cancellation_workflow_id_for(request)
        updated = connection.execute(
            agent_attempts.update()
            .where(
                agent_attempts.c.attempt_id == attempt.attempt_id.value,
                agent_attempts.c.state == attempt.state.value,
                agent_attempts.c.state_version == attempt.state_version,
                agent_attempts.c.cancellation_command_id.is_(None),
            )
            .values(
                state=AgentAttemptState.CANCEL_REQUESTED.value,
                state_version=attempt.state_version + 1,
                cancellation_command_id=request.command_id,
                cancellation_expected_state_version=(
                    request.expected_attempt_state_version
                ),
                replacement=request.replacement.value,
                redrive_state=AgentAttemptRedriveState.PENDING.value,
                cancellation_workflow_id=workflow_id,
            )
        )
        if updated.rowcount != 1:
            return None
        accepted = _load_attempt(connection, attempt.attempt_id)
        _insert_attempt_event(
            connection,
            accepted,
            RunEventKind.AGENT_CANCEL_REQUESTED,
            command=accepted.cancellation,
        )
        if self._application_version is None:
            raise RunTransitionConflict(
                "cancellation submission requires the runtime application version"
            )
        client = DBOSClient(
            system_database_engine=self._engine, use_listen_notify=False
        )
        try:
            options: EnqueueOptions = {
                "workflow_name": CANCELLATION_WORKFLOW_NAME,
                "queue_name": QUEUE_NAME,
                "workflow_id": workflow_id,
                "app_version": self._application_version,
            }
            client.enqueue_in_transaction(
                connection,
                options,
                attempt.run_id.value,
                attempt.attempt_id.value,
                request.command_id,
            )
        finally:
            client.destroy()
        return accepted

    def attest_cancellation_cleanup(
        self,
        request: CancelAgentAttemptRequest,
        disposition: AgentAttemptCancellationDisposition,
        process_owner_id: AgentProcessOwnerId | None,
        watchdog_generation_id: WatchdogGenerationId | None,
    ) -> AgentAttemptCancellationAccepted:
        with canonical_write_transaction(self._engine) as connection:
            attempt = _load_attempt(connection, request.attempt_id)
            if attempt.runner_manifest_id is not None:
                raise RunTransitionConflict(
                    "runner-bound cancellation cleanup requires Runner evidence"
                )
            cancellation = attempt.cancellation
            if cancellation is None or not cancellation.matches(request):
                raise RunTransitionConflict(
                    "cleanup attestation differs from its cancellation command"
                )
            if attempt.state in {
                AgentAttemptState.CANCELLED,
                AgentAttemptState.INTERRUPTED,
            }:
                if cancellation.disposition is not disposition:
                    raise RunTransitionConflict(
                        "cleanup retry differs from durable disposition"
                    )
                return AgentAttemptCancellationAccepted(
                    attempt,
                    True,
                    self._replacement_attempt_id(connection, attempt),
                )
            if attempt.state is not AgentAttemptState.CANCEL_REQUESTED:
                raise RunTransitionConflict("only a requested cancellation can attest")
            if (
                attempt.process_owner_id != process_owner_id
                or attempt.watchdog_generation_id != watchdog_generation_id
            ):
                raise RunTransitionConflict(
                    "cleanup attestation differs from durable owner generation"
                )
            terminal_state = (
                AgentAttemptState.INTERRUPTED
                if disposition
                is AgentAttemptCancellationDisposition.OWNER_LOST_AFTER_PARENT_DEATH
                else AgentAttemptState.CANCELLED
            )
            terminal_cancellation = AgentAttemptCancellation(
                cancellation.command_id,
                cancellation.expected_attempt_state_version,
                cancellation.replacement,
                AgentAttemptRedriveState.CLEANUP_ATTESTED,
                disposition,
            )
            updated = connection.execute(
                agent_attempts.update()
                .where(
                    agent_attempts.c.attempt_id == attempt.attempt_id.value,
                    agent_attempts.c.state == AgentAttemptState.CANCEL_REQUESTED.value,
                    agent_attempts.c.state_version == attempt.state_version,
                    agent_attempts.c.cancellation_command_id == request.command_id,
                )
                .values(
                    state=terminal_state.value,
                    state_version=attempt.state_version + 1,
                    process_phase=AgentAttemptProcessPhase.CLEANUP_ATTESTED.value,
                    process_owner_id=(
                        None if process_owner_id is None else process_owner_id.value
                    ),
                    watchdog_generation_id=(
                        None
                        if watchdog_generation_id is None
                        else watchdog_generation_id.value
                    ),
                    redrive_state=AgentAttemptRedriveState.CLEANUP_ATTESTED.value,
                    cancellation_disposition=disposition.value,
                )
            )
            if updated.rowcount != 1:
                raise RunTransitionConflict("cleanup attestation lost its attempt CAS")
            record_attempt_ended(connection, attempt.attempt_id.value)
            terminal = _load_attempt(connection, attempt.attempt_id)
            replacement_attempt_id = None
            if cancellation.replacement is AgentAttemptReplacement.ONE:
                replacement_attempt_id = AgentAttemptId.for_execution(
                    attempt.node_execution_id, attempt.request_hash, 2
                )
                replacement = AgentAttempt(
                    replacement_attempt_id,
                    attempt.node_execution_id,
                    attempt.request_hash,
                    attempt.executor_operational_identity,
                    attempt.run_id,
                    attempt.workflow_revision_hash,
                    attempt.node_id,
                    2,
                    AgentAttemptState.PREPARED,
                    0,
                )
                connection.execute(
                    agent_attempts.insert().values(_attempt_values(replacement))
                )
                record_attempt_started(connection, replacement.attempt_id.value)
                if self._application_version is None:
                    raise RunTransitionConflict(
                        "replacement submission requires the runtime application version"
                    )
                client = DBOSClient(
                    system_database_engine=self._engine, use_listen_notify=False
                )
                try:
                    options: EnqueueOptions = {
                        "workflow_name": REPLACEMENT_WORKFLOW_NAME,
                        "queue_name": QUEUE_NAME,
                        "workflow_id": replacement_workflow_id_for(
                            replacement_attempt_id
                        ),
                        "app_version": self._application_version,
                    }
                    client.enqueue_in_transaction(
                        connection, options, replacement_attempt_id.value
                    )
                finally:
                    client.destroy()
            _insert_attempt_event(
                connection,
                terminal,
                (
                    RunEventKind.AGENT_INTERRUPTED
                    if terminal_state is AgentAttemptState.INTERRUPTED
                    else RunEventKind.AGENT_CANCELLED
                ),
                command=terminal_cancellation,
                replacement_attempt_id=replacement_attempt_id,
            )
            _lift_run_under_operator_cancel(connection, terminal)
            return AgentAttemptCancellationAccepted(
                terminal, True, replacement_attempt_id
            )

    def mark_cancellation_owner_not_local(
        self, request: CancelAgentAttemptRequest
    ) -> AgentAttempt:
        with canonical_write_transaction(self._engine) as connection:
            attempt = _load_attempt(connection, request.attempt_id)
            cancellation = attempt.cancellation
            if cancellation is None or not cancellation.matches(request):
                raise RunTransitionConflict(
                    "owner redrive differs from its cancellation command"
                )
            if cancellation.redrive_state is AgentAttemptRedriveState.OWNER_NOT_LOCAL:
                return attempt
            if (
                attempt.state is not AgentAttemptState.CANCEL_REQUESTED
                or cancellation.redrive_state is not AgentAttemptRedriveState.PENDING
            ):
                raise RunTransitionConflict(
                    "only a pending cancellation can lose its local owner"
                )
            updated = connection.execute(
                agent_attempts.update()
                .where(
                    agent_attempts.c.attempt_id == attempt.attempt_id.value,
                    agent_attempts.c.state == AgentAttemptState.CANCEL_REQUESTED.value,
                    agent_attempts.c.state_version == attempt.state_version,
                    agent_attempts.c.redrive_state
                    == AgentAttemptRedriveState.PENDING.value,
                )
                .values(
                    state_version=attempt.state_version + 1,
                    redrive_state=AgentAttemptRedriveState.OWNER_NOT_LOCAL.value,
                )
            )
            if updated.rowcount != 1:
                raise RunTransitionConflict("owner redrive lost its attempt CAS")
            return _load_attempt(connection, attempt.attempt_id)

    @staticmethod
    def _replacement_attempt_id(
        connection: Any, attempt: AgentAttempt
    ) -> AgentAttemptId | None:
        if (
            attempt.cancellation is None
            or attempt.cancellation.replacement is AgentAttemptReplacement.NONE
        ):
            return None
        value = connection.scalar(
            sa.select(agent_attempts.c.attempt_id).where(
                agent_attempts.c.node_execution_id == attempt.node_execution_id.value,
                agent_attempts.c.attempt_ordinal == 2,
            )
        )
        return None if value is None else AgentAttemptId(str(value))
