"""Projection of a durable run onto the wire schema this API serves."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, assert_never, cast

from atelier2.api.projection.workflows import UnservedWorkflowFormat
from atelier2.api.references import (
    MAXIMUM_RUN_TERMINAL_ANSWER_BYTES,
    encode_canonical_base64,
    encode_event_cursor,
    encode_public_run_reference,
)
from atelier2.api.wire.resources import (
    AgentBindingResourceV2,
    AssistantTurnEventResource,
    AttemptTranscriptResource,
    DefectiveRunRowResource,
    NodeAnswerResource,
    NodeDetailResource,
    NodeProvenanceResource,
    NodeRailAttemptResource,
    NodeRailResource,
    NodeRefusalOutputResource,
    NodeStateName,
    ProviderTerminalRefusalEventResource,
    PublicAttemptStateName,
    RunCancellabilityResource,
    RunForkOriginResource,
    RunForkSuccessorResource,
    RunListRowResource,
    RunNotCancellableReasonName,
    RunOrderResource,
    RunResourceV3,
    RunTerminalAnswerOmissionReasonName,
    RunTerminalAnswerOmittedResource,
    RunTerminalAnswerValueResource,
    ToolCalledEventResource,
    ToolReturnedEventResource,
    TranscriptBeforeMomentsOrigin,
    TranscriptBeforeMomentsResource,
    TranscriptMomentResource,
    TranscriptRecordedMomentResource,
    TranscriptTruncatedEventResource,
    UnrecognisedProviderOutputEventResource,
    UsageEventResource,
)
from atelier2.application.project_node_rail import NodeRailEntry, project_node_rail
from atelier2.contracts.agent_transcripts import (
    AssistantTurn,
    AttemptTranscript,
    ProviderTerminalRefusal,
    ToolCalled,
    ToolReturned,
    TranscriptBeforeMoments,
    TranscriptEvent,
    TranscriptEventKind,
    TranscriptEventMoment,
    TranscriptMomentOrigin,
    TranscriptRecordedMoment,
    TranscriptTruncated,
    UnrecognisedProviderOutput,
    Usage,
)
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.node_records_v3 import RunInput
from atelier2.contracts.run_bindings import RunV3
from atelier2.contracts.run_projections import (
    DefectiveRunProjection,
    NodeAnswer,
    NodeDetail,
    PublicAgentAttemptState,
    RunCancellationRefusal,
    RunProjection,
)
from atelier2.contracts.runs import RunState
from atelier2.contracts.work_items import (
    WORK_ITEM_ORDER_SCHEMA_REVISION,
    read_work_item_order_document,
)
from atelier2.contracts.workflows_v3 import AgentNodeV3

_LIVE_ATTEMPT_STATES = frozenset(
    {PublicAgentAttemptState.PREPARED, PublicAgentAttemptState.POSSIBLY_RAN}
)


def node_rail_resources(
    entries: Sequence[NodeRailEntry],
) -> tuple[NodeRailResource, ...]:
    """The one mapping of the derived rail onto the wire, for a run and an event."""
    return tuple(
        NodeRailResource(
            node_id=entry.node_id,
            state=cast(NodeStateName, entry.state),
            attempt=(
                None
                if entry.attempt is None
                else NodeRailAttemptResource(
                    ordinal=cast(Literal[1, 2], entry.attempt.ordinal),
                    state=cast(PublicAttemptStateName | None, entry.attempt.state),
                )
            ),
            reused_from_run_reference=(
                None
                if entry.reused is None
                else encode_public_run_reference(entry.reused.source_run_id)
            ),
            source_event_hash=(
                None if entry.reused is None else entry.reused.source_event_hash.value
            ),
            source_receipt_hash=(
                None if entry.reused is None else entry.reused.source_receipt_hash.value
            ),
            source_declared_context_package_hash=(
                None
                if entry.reused is None
                else entry.reused.source_declared_context_package_hash.value
            ),
        )
        for entry in entries
    )


def run_order_resource(order: RunInput) -> RunOrderResource:
    """One stored order, told safely: never its own bytes, only their shape."""
    return RunOrderResource(
        name=order.name,
        bytes=len(order.value),
        schema_revision_hash=order.schema_revision.value,
    )


class WorkItemOrderDurableStateCorrupt(ValueError):
    """A run's own order pins the work-item schema but is not that document.

    A `ValueError` because that is what corrupt durable state already raises
    as, here and at `UnservedWorkflowFormat` beside it: `WORK_ITEM_ORDER_SCHEMA_REVISION`
    is the store's own admission gate (a start refuses any other schema under
    it, `contracts.work_items`), so an order that names it and still fails
    `read_work_item_order_document` is the store disagreeing with itself, not
    a caller's mistake this reader may quietly fold into `None`.
    """


def run_work_item_reference(orders: Sequence[RunInput]) -> str | None:
    """The tracker reference one of this run's own orders names, or none (#1045).

    An order counts only when it pins `WORK_ITEM_ORDER_SCHEMA_REVISION` --
    the exact pin a start already refuses to admit any other schema under
    (`contracts.work_items`) -- and its bytes are read through
    `read_work_item_order_document`, the one owner that already wrote them,
    never a second guess parsed from a node's composed job text. The first
    such order wins; a workflow that starts more than one names the same run
    purpose either way.
    """
    for order in orders:
        if order.schema_revision != WORK_ITEM_ORDER_SCHEMA_REVISION:
            continue
        document = read_work_item_order_document(order.value)
        if document is None:
            raise WorkItemOrderDurableStateCorrupt(
                "a work-item-schema order's bytes are not a work-item order document"
            )
        return document.reference.value
    return None


def run_terminal_answer_resource(
    answer: NodeAnswer | None,
) -> RunTerminalAnswerValueResource | RunTerminalAnswerOmittedResource | None:
    """The terminal node's accepted answer, bounded for a listed run row (#1045).

    `None` only where the run named no answer at all. A value larger than
    `MAXIMUM_RUN_TERMINAL_ANSWER_BYTES` allows on a list row is a different
    fact -- the node did write one, this row just does not carry it -- so it
    answers `RunTerminalAnswerOmittedResource` naming the bound, never a bare
    `None` a reader could mistake for absence. The run's own node detail
    route still reads the value in full, never omitting it.
    """
    if answer is None:
        return None
    if len(answer.value) > MAXIMUM_RUN_TERMINAL_ANSWER_BYTES:
        return RunTerminalAnswerOmittedResource(
            kind="omitted",
            reason=RunTerminalAnswerOmissionReasonName.TOO_LARGE,
            maximum_bytes=MAXIMUM_RUN_TERMINAL_ANSWER_BYTES,
        )
    return RunTerminalAnswerValueResource(
        kind="value",
        value_base64=encode_canonical_base64(answer.value),
        value_hash=answer.value_hash.value,
    )


def run_terminal_refusal_output_resource(
    refusal_output: NodeAnswer | None,
) -> NodeRefusalOutputResource | None:
    """The terminal node's already-redacted schema refusal (#1045, #664)."""
    if refusal_output is None:
        return None
    return NodeRefusalOutputResource(
        value_base64=encode_canonical_base64(refusal_output.value),
        value_hash=refusal_output.value_hash.value,
    )


def _run_not_cancellable_reason(
    projection: RunProjection, run: RunV3
) -> RunCancellationRefusal | None:
    """Why this run cannot be operator-cancelled now, or nothing when it can.

    The closed predicate #439 D3 makes the server own: a run is cancellable
    while it is STARTED on an agent node whose live attempt this cancel could
    stop, and while it rests at a pause nobody has answered (#668) -- a resting
    Wait ends under its own attestation rather than standing owed an answer
    forever. Every other standing is a named reason, never a silent no.

    A pause is still the operator's move, so `WAITING_FOR_YOU` keeps naming the
    reconciliation a person has to resolve: that one has a live platform-effect
    intent behind it, and ending the run there would abandon it.
    """
    if run.state in {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}:
        return RunCancellationRefusal.ALREADY_ENDED
    if run.state is RunState.WAITING_RECONCILIATION:
        return RunCancellationRefusal.WAITING_FOR_YOU
    if run.state is RunState.WAITING_INPUT:
        return None
    if not isinstance(projection.graph.node(run.current_node_id), AgentNodeV3):
        return RunCancellationRefusal.NODE_RUNS_NO_AGENT
    attempt = projection.current_agent_attempt
    if attempt is None or attempt.state not in _LIVE_ATTEMPT_STATES:
        if attempt is not None and attempt.state is (
            PublicAgentAttemptState.CANCEL_REQUESTED
        ):
            return RunCancellationRefusal.ALREADY_CANCELLING
        return RunCancellationRefusal.BETWEEN_NODES
    return None


def _run_cancellability(
    projection: RunProjection, run: RunV3
) -> RunCancellabilityResource:
    reason = _run_not_cancellable_reason(projection, run)
    if reason is not None:
        return RunCancellabilityResource(
            cancellable=False,
            reason=cast(RunNotCancellableReasonName, reason.value),
            target_node_execution_id=None,
        )
    # The fence is the run's own live execution, derived the way the store
    # recomputes it before it accepts a command (#439 D2). An agent node's live
    # attempt was looked up by this very identity, so naming it here instead of
    # the attempt's copy keeps one derivation -- and it is the only honest
    # answer for a resting Wait, whose last agent attempt belongs to some
    # earlier node entirely.
    return RunCancellabilityResource(
        cancellable=True,
        reason=None,
        target_node_execution_id=NodeExecutionId.for_node(
            run.run_id,
            run.revision_hash,
            run.current_node_id,
            run.current_round_ordinal,
        ).value,
    )


def run_resource(projection: RunProjection) -> RunResourceV3:
    """One run rendered in its own shape, along the edge its author declared.

    Every run this API serves is format 3, so a run binding of any other
    generation is durable state the wire has no shape for and is refused rather
    than bent into one.
    """

    run = projection.run
    if not isinstance(run, RunV3):
        raise UnservedWorkflowFormat("the API projects format-3 runs only")
    return RunResourceV3(
        workflow_format_version=3,
        run_id=run.run_id.value,
        public_run_reference=encode_public_run_reference(run.run_id),
        workflow_revision_hash=run.revision_hash.value,
        workflow_name=projection.graph.name,
        agent_binding_set_hash=run.binding_set_hash.value,
        run_configuration_revision_hash=run.run_configuration_revision_hash.value,
        agent_bindings=tuple(
            AgentBindingResourceV2(
                role=binding.role.value,
                agent_configuration_revision_hash=(
                    binding.configuration.revision_hash.value
                ),
                auth_profile_revision_hash=binding.auth_profile.revision_hash.value,
                profile_id=binding.auth_profile.profile_id,
                revision_number=binding.auth_profile.revision_number,
                provider_id=binding.auth_profile.provider_id.value,
                auth_mode=binding.auth_profile.auth_mode.value,
                model=binding.configuration.model,
                executor_revision=binding.configuration.executor_revision.value,
            )
            for binding in run.agent_bindings
        ),
        orders=tuple(run_order_resource(order) for order in projection.orders),
        work_item_reference=run_work_item_reference(projection.orders),
        fork_origin=(
            None
            if projection.fork_origin is None
            else RunForkOriginResource(
                public_run_reference=encode_public_run_reference(
                    projection.fork_origin.origin_run_id
                ),
                terminal_hash=projection.fork_origin.terminal_hash.value,
                restart_from_node_id=projection.fork_origin.restart_from_node_id,
                fork_hash=projection.fork_origin.fork_hash.value,
            )
        ),
        fork_successors=tuple(
            RunForkSuccessorResource(
                public_run_reference=encode_public_run_reference(
                    successor.successor_run_id
                ),
                restart_from_node_id=successor.restart_from_node_id,
                fork_hash=successor.fork_hash.value,
            )
            for successor in projection.fork_successors
        ),
        answer=run_terminal_answer_resource(projection.answer),
        refusal_output=run_terminal_refusal_output_resource(projection.refusal_output),
        state_version=run.state_version,
        state=cast(
            Literal[
                "STARTED",
                "WAITING_RECONCILIATION",
                "WAITING_INPUT",
                "COMPLETED",
                "FAILED",
                "CANCELLED",
            ],
            run.state.value,
        ),
        current_node_id=run.current_node_id,
        current_node_execution_id=NodeExecutionId.for_node(
            run.run_id,
            run.revision_hash,
            run.current_node_id,
            run.current_round_ordinal,
        ).value,
        # A run resource says where the snapshot stands, so no event has
        # overtaken it here; the event stream carries its own rail.
        node_rail=node_rail_resources(project_node_rail(projection, ())),
        cancellation=_run_cancellability(projection, run),
        terminal_hash=None if run.terminal_hash is None else run.terminal_hash.value,
        latest_event_cursor=(
            None
            if run.last_event_sequence == 0
            else encode_event_cursor(run.run_id, run.last_event_sequence)
        ),
        started_at=None
        if projection.started_at is None
        else projection.started_at.value,
        ended_at=None if projection.ended_at is None else projection.ended_at.value,
    )


def run_list_row_resource(
    row: RunProjection | DefectiveRunProjection,
) -> RunListRowResource | DefectiveRunRowResource:
    """One listed row, whichever of the two shapes its own projection earned.

    A listing answers for every run it can, so the row a corrupt entry
    becomes is told apart from a run resource by `kind` rather than left for
    a reader to infer from missing fields (#1042).
    """

    if isinstance(row, DefectiveRunProjection):
        return DefectiveRunRowResource(
            kind="defective",
            public_run_reference=encode_public_run_reference(row.run_id),
            problem_code=cast(Literal["durable-state-corrupt"], row.problem_code.value),
            detail=row.detail,
        )
    return RunListRowResource(kind="run", run=run_resource(row))


def node_detail_resource(detail: NodeDetail) -> NodeDetailResource:
    """One node detail on the wire, with every absence kept as an absence."""

    return NodeDetailResource(
        run_id=detail.run_id.value,
        public_run_reference=encode_public_run_reference(detail.run_id),
        node_id=detail.node_id,
        state=cast(NodeStateName, detail.state.value),
        job_base64=None if detail.job is None else encode_canonical_base64(detail.job),
        job_hash=detail.job_hash,
        answer=None
        if detail.answer is None
        else NodeAnswerResource(
            value_base64=encode_canonical_base64(detail.answer.value),
            value_hash=detail.answer.value_hash.value,
        ),
        provenance=None
        if detail.provenance is None
        else NodeProvenanceResource(
            role=detail.provenance.role,
            provider_id=detail.provenance.provider_id,
            model=detail.provenance.model,
            executor_revision=detail.provenance.executor_revision,
            executor_operational_identity=(
                detail.provenance.executor_operational_identity
            ),
            auth_mode=detail.provenance.auth_mode,
            profile_id=detail.provenance.profile_id,
            agent_configuration_revision_hash=(
                detail.provenance.agent_configuration_revision_hash
            ),
            request_hash=detail.provenance.request_hash,
            receipt_hash=detail.provenance.receipt_hash,
        ),
        refusal=detail.refusal,
        refusal_output=None
        if detail.refusal_output is None
        else NodeRefusalOutputResource(
            value_base64=encode_canonical_base64(detail.refusal_output.value),
            value_hash=detail.refusal_output.value_hash.value,
        ),
        started_at=None if detail.started_at is None else detail.started_at.value,
        ended_at=None if detail.ended_at is None else detail.ended_at.value,
        transcript=(
            None
            if detail.transcript is None
            else attempt_transcript_resource(detail.transcript)
        ),
    )


def attempt_transcript_resource(
    transcript: AttemptTranscript,
) -> AttemptTranscriptResource:
    """The decoded events on the wire, without the stored document envelope."""

    return AttemptTranscriptResource(
        events=tuple(_transcript_event_resource(event) for event in transcript.events)
    )


def _transcript_event_resource(
    event: TranscriptEvent,
) -> (
    ToolCalledEventResource
    | ToolReturnedEventResource
    | AssistantTurnEventResource
    | UsageEventResource
    | ProviderTerminalRefusalEventResource
    | UnrecognisedProviderOutputEventResource
    | TranscriptTruncatedEventResource
):
    match event:
        case ToolCalled(name, arguments, redacted, moment):
            return ToolCalledEventResource(
                event=TranscriptEventKind.TOOL_CALLED,
                name=name,
                arguments=arguments,
                redacted=redacted,
                moment=_transcript_moment_resource(moment),
            )
        case ToolReturned(name, result, redacted, moment):
            return ToolReturnedEventResource(
                event=TranscriptEventKind.TOOL_RETURNED,
                name=name,
                result=result,
                redacted=redacted,
                moment=_transcript_moment_resource(moment),
            )
        case AssistantTurn(text, redacted, moment):
            return AssistantTurnEventResource(
                event=TranscriptEventKind.ASSISTANT_TURN,
                text=text,
                redacted=redacted,
                moment=_transcript_moment_resource(moment),
            )
        case Usage(input_tokens, output_tokens, cache_read, cache_creation, moment):
            return UsageEventResource(
                event=TranscriptEventKind.USAGE,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_input_tokens=cache_read,
                cache_creation_input_tokens=cache_creation,
                moment=_transcript_moment_resource(moment),
            )
        case ProviderTerminalRefusal(
            terminal_reason, api_error_status, text, redacted, moment
        ):
            return ProviderTerminalRefusalEventResource(
                event=TranscriptEventKind.PROVIDER_TERMINAL_REFUSAL,
                terminal_reason=terminal_reason,
                api_error_status=api_error_status,
                text=text,
                redacted=redacted,
                moment=_transcript_moment_resource(moment),
            )
        case UnrecognisedProviderOutput(text, redacted, moment):
            return UnrecognisedProviderOutputEventResource(
                event=TranscriptEventKind.UNRECOGNISED_PROVIDER_OUTPUT,
                text=text,
                redacted=redacted,
                moment=_transcript_moment_resource(moment),
            )
        case TranscriptTruncated(dropped_events, moment):
            return TranscriptTruncatedEventResource(
                event=TranscriptEventKind.TRANSCRIPT_TRUNCATED,
                dropped_events=dropped_events,
                moment=_transcript_moment_resource(moment),
            )
        case _ as unreachable:
            assert_never(unreachable)


def _transcript_moment_resource(
    moment: TranscriptEventMoment,
) -> TranscriptMomentResource:
    """The event's recording fact, including the explicit legacy absence."""

    match moment:
        case TranscriptRecordedMoment(recorded_at, TranscriptMomentOrigin.RECORDED):
            return TranscriptRecordedMomentResource(
                recorded_at=recorded_at.value,
                origin=TranscriptMomentOrigin.RECORDED,
            )
        case TranscriptBeforeMoments():
            return TranscriptBeforeMomentsResource(
                origin=TranscriptBeforeMomentsOrigin.V1
            )
        case _ as unreachable:
            assert_never(unreachable)
