"""Projection of a persisted run event onto its wire schema, in either version."""

from __future__ import annotations

from typing import Literal, cast

from atelier2.api.projection.workflows import receipt_resource
from atelier2.api.references import (
    encode_canonical_base64,
    encode_event_cursor,
    encode_public_run_reference,
)
from atelier2.api.wire.events import (
    ActionCompletedEventResource,
    ActionCompletedEventResourceV2,
    ActionReconciliationRequiredEventResource,
    ActionReconciliationRequiredEventResourceV2,
    ActionReconciliationResolvedEventResource,
    ActionReconciliationResolvedEventResourceV2,
    AgentCancelledEventResourceV2,
    AgentCancelledEventResourceV3,
    AgentCancelRequestedEventResourceV2,
    AgentCancelRequestedEventResourceV3,
    AgentCompletedEventResource,
    AgentCompletedEventResourceV2,
    AgentCompletedEventResourceV3,
    AgentFailedEventResourceV2,
    AgentFailedEventResourceV3,
    AgentInterruptedEventResourceV2,
    AgentInterruptedEventResourceV3,
    AnyRunEventResource,
    RunEventResourceV2,
    RunEventResourceV3,
    SubworkflowCompletedEventResource,
    SubworkflowCompletedEventResourceV2,
    WaitAnsweredEventResource,
    WaitAnsweredEventResourceV2,
    WaitingInputEventResource,
    WaitingInputEventResourceV2,
)
from atelier2.api.wire.resources import NodeRailResource
from atelier2.contracts.executions import (
    KINDS_NO_V1_RUN_CARRIES,
    RunEventKind,
    is_canonical_integer_bytes,
)
from atelier2.contracts.run_events import PersistedRunEvent


def run_event_resource(
    projection: PersistedRunEvent, node_rail: tuple[NodeRailResource, ...]
) -> AnyRunEventResource:
    """One durable event on the wire, carrying where its run stands after it.

    Each family answers in its own shape. The rail does not reach a V1 resource:
    that schema is byte-frozen, so a V1 reader keeps deriving and the rail passed
    here is dropped. A format-3 event used to fall through to that same V1 branch
    -- saying nothing about its format and losing the rail -- which left a cockpit
    reading the stream of a run it had just started unable to tell which family it
    was looking at.
    """

    if projection.workflow_format_version == 2:
        return _run_event_resource_v2(projection, node_rail)
    if projection.workflow_format_version == 3:
        return _run_event_resource_v3(projection, node_rail)
    event = projection.event
    if event.event_kind in KINDS_NO_V1_RUN_CARRIES:
        raise ValueError(f"a V1 run cannot carry {event.event_kind.value}")
    common = {
        "cursor": encode_event_cursor(event.run_id, event.event_sequence),
        "sequence": event.event_sequence,
        "public_run_reference": encode_public_run_reference(event.run_id),
        "workflow_revision_hash": event.revision_hash.value,
        "node_id": event.node_id,
        "node_execution_id": event.node_execution_id.value,
        "event_hash": event.event_hash.value,
    }
    if event.event_kind is RunEventKind.AGENT_COMPLETED:
        return AgentCompletedEventResource(
            event=event.event_kind.value,
            output=event.payload.decode("utf-8"),
            payload_hash=event.payload_hash.value,
            **common,
        )
    if event.event_kind is RunEventKind.ACTION_RECONCILIATION_REQUIRED:
        return ActionReconciliationRequiredEventResource(
            event=event.event_kind.value,
            request_base64=encode_canonical_base64(event.payload),
            request_hash=event.payload_hash.value,
            **common,
        )
    if event.event_kind is RunEventKind.ACTION_RECONCILIATION_RESOLVED:
        if projection.receipt is None:
            raise ValueError("resolved event has no receipt")
        return ActionReconciliationResolvedEventResource(
            event=event.event_kind.value,
            receipt=receipt_resource(projection.receipt),
            **common,
        )
    if event.event_kind is RunEventKind.ACTION_COMPLETED:
        if projection.receipt is None:
            raise ValueError("completed Action event has no receipt")
        return ActionCompletedEventResource(
            event=event.event_kind.value,
            receipt=receipt_resource(projection.receipt),
            **common,
        )
    if event.event_kind is RunEventKind.WAITING_INPUT:
        return WaitingInputEventResource(
            event=event.event_kind.value, answer_type="integer", **common
        )
    if event.event_kind is RunEventKind.WAIT_ANSWERED:
        if not is_canonical_integer_bytes(event.payload):
            raise ValueError("durable wait answer is not canonical integer text")
        answer = event.payload.decode("ascii")
        return WaitAnsweredEventResource(
            event=event.event_kind.value,
            answer=answer,
            answer_hash=event.payload_hash.value,
            **common,
        )
    if event.event_kind is RunEventKind.SUBWORKFLOW_COMPLETED:
        if not is_canonical_integer_bytes(event.payload):
            raise ValueError("durable subworkflow result is not canonical integer text")
        return SubworkflowCompletedEventResource(
            event=event.event_kind.value,
            result=int(event.payload.decode("ascii")),
            result_hash=event.payload_hash.value,
            **common,
        )
    raise AssertionError("closed event union was extended without API mapping")


def _run_event_resource_v3(
    projection: PersistedRunEvent, node_rail: tuple[NodeRailResource, ...]
) -> RunEventResourceV3:
    """One durable event of a format-3 run, in the shape that says so.

    A V3 node is an Agent, so the five agent kinds are the whole vocabulary a V3
    run can write. Anything else in the row is a store that disagrees with the
    document it names, and it is refused rather than rendered as something a
    reader would believe.
    """

    event = projection.event
    common = {
        "workflow_format_version": 3,
        "node_rail": node_rail,
        "cursor": encode_event_cursor(event.run_id, event.event_sequence),
        "sequence": event.event_sequence,
        "public_run_reference": encode_public_run_reference(event.run_id),
        "workflow_revision_hash": event.revision_hash.value,
        "node_id": event.node_id,
        "node_execution_id": event.node_execution_id.value,
        "event_hash": event.event_hash.value,
    }
    if event.agent_attempt_id is None or event.attempt_ordinal is None:
        raise ValueError(f"a V3 {event.event_kind.value} has no exact attempt binding")
    attempt = {
        "attempt_id": event.agent_attempt_id,
        "attempt_ordinal": cast(Literal[1, 2], event.attempt_ordinal),
    }
    if event.event_kind is RunEventKind.AGENT_COMPLETED:
        return AgentCompletedEventResourceV3(
            event=event.event_kind.value,
            output_base64=encode_canonical_base64(event.payload),
            output_hash=event.payload_hash.value,
            **attempt,
            **common,
        )
    if event.event_kind is RunEventKind.AGENT_FAILED:
        if event.payload.decode("ascii") != "PROCESS_EXITED_UNSUCCESSFULLY":
            raise ValueError("durable agent failure payload is not canonical")
        return AgentFailedEventResourceV3(
            event=event.event_kind.value,
            failure_code="PROCESS_EXITED_UNSUCCESSFULLY",
            **attempt,
            **common,
        )
    if event.cancellation_command_id is None or event.replacement is None:
        raise ValueError(
            f"a V3 {event.event_kind.value} carries no cancellation binding"
        )
    cancellation = {
        "command_id": event.cancellation_command_id,
        "replacement": cast(Literal["NONE", "ONE"], event.replacement),
    }
    if event.event_kind is RunEventKind.AGENT_CANCEL_REQUESTED:
        return AgentCancelRequestedEventResourceV3(
            event=event.event_kind.value, **cancellation, **attempt, **common
        )
    if event.event_kind not in {
        RunEventKind.AGENT_CANCELLED,
        RunEventKind.AGENT_INTERRUPTED,
    }:
        raise ValueError(f"a V3 run cannot carry {event.event_kind.value}")
    if event.cancellation_disposition is None:
        raise ValueError("a V3 cancellation terminal has no disposition")
    terminal = {
        "disposition": cast(
            Literal[
                "NEVER_LAUNCHED",
                "EXITED_BEFORE_SIGNAL",
                "REAPED_AFTER_TERM",
                "REAPED_AFTER_KILL",
                "OWNER_LOST_AFTER_PARENT_DEATH",
            ],
            event.cancellation_disposition,
        ),
        "replacement_attempt_id": event.replacement_attempt_id,
    }
    if event.event_kind is RunEventKind.AGENT_CANCELLED:
        return AgentCancelledEventResourceV3(
            event=event.event_kind.value,
            **terminal,
            **cancellation,
            **attempt,
            **common,
        )
    return AgentInterruptedEventResourceV3(
        event="AGENT_INTERRUPTED", **terminal, **cancellation, **attempt, **common
    )


def _run_event_resource_v2(
    projection: PersistedRunEvent, node_rail: tuple[NodeRailResource, ...]
) -> RunEventResourceV2:
    event = projection.event
    common = {
        "workflow_format_version": 2,
        "node_rail": node_rail,
        "cursor": encode_event_cursor(event.run_id, event.event_sequence),
        "sequence": event.event_sequence,
        "public_run_reference": encode_public_run_reference(event.run_id),
        "workflow_revision_hash": event.revision_hash.value,
        "node_id": event.node_id,
        "node_execution_id": event.node_execution_id.value,
        "event_hash": event.event_hash.value,
    }
    if event.event_kind is RunEventKind.AGENT_COMPLETED:
        if event.agent_attempt_id is None or event.attempt_ordinal is None:
            raise ValueError("V2 agent completion has no exact attempt binding")
        return AgentCompletedEventResourceV2(
            event=event.event_kind.value,
            output_base64=encode_canonical_base64(event.payload),
            output_hash=event.payload_hash.value,
            attempt_id=event.agent_attempt_id,
            attempt_ordinal=cast(Literal[1, 2], event.attempt_ordinal),
            **common,
        )
    if event.event_kind is RunEventKind.AGENT_FAILED:
        failure_code = event.payload.decode("ascii")
        if failure_code != "PROCESS_EXITED_UNSUCCESSFULLY":
            raise ValueError("durable agent failure payload is not canonical")
        if event.agent_attempt_id is None or event.attempt_ordinal is None:
            raise ValueError("V2 agent failure has no exact attempt binding")
        return AgentFailedEventResourceV2(
            event=event.event_kind.value,
            failure_code="PROCESS_EXITED_UNSUCCESSFULLY",
            attempt_id=event.agent_attempt_id,
            attempt_ordinal=cast(Literal[1, 2], event.attempt_ordinal),
            **common,
        )
    if event.event_kind is RunEventKind.AGENT_CANCEL_REQUESTED:
        if (
            event.agent_attempt_id is None
            or event.attempt_ordinal is None
            or event.cancellation_command_id is None
            or event.replacement is None
        ):
            raise ValueError("V2 cancellation request has no exact binding")
        return AgentCancelRequestedEventResourceV2(
            event=event.event_kind.value,
            attempt_id=event.agent_attempt_id,
            attempt_ordinal=cast(Literal[1, 2], event.attempt_ordinal),
            command_id=event.cancellation_command_id,
            replacement=cast(Literal["NONE", "ONE"], event.replacement),
            **common,
        )
    if event.event_kind in {
        RunEventKind.AGENT_CANCELLED,
        RunEventKind.AGENT_INTERRUPTED,
    }:
        if (
            event.agent_attempt_id is None
            or event.attempt_ordinal is None
            or event.cancellation_command_id is None
            or event.replacement is None
            or event.cancellation_disposition is None
        ):
            raise ValueError("V2 cancellation terminal has no exact binding")
        terminal_common = {
            "attempt_id": event.agent_attempt_id,
            "attempt_ordinal": cast(Literal[1, 2], event.attempt_ordinal),
            "command_id": event.cancellation_command_id,
            "replacement": cast(Literal["NONE", "ONE"], event.replacement),
            "disposition": cast(
                Literal[
                    "NEVER_LAUNCHED",
                    "EXITED_BEFORE_SIGNAL",
                    "REAPED_AFTER_TERM",
                    "REAPED_AFTER_KILL",
                    "OWNER_LOST_AFTER_PARENT_DEATH",
                ],
                event.cancellation_disposition,
            ),
            "replacement_attempt_id": event.replacement_attempt_id,
        }
        if event.event_kind is RunEventKind.AGENT_CANCELLED:
            return AgentCancelledEventResourceV2(
                event=event.event_kind.value, **terminal_common, **common
            )
        return AgentInterruptedEventResourceV2(
            event="AGENT_INTERRUPTED", **terminal_common, **common
        )
    if event.event_kind is RunEventKind.ACTION_RECONCILIATION_REQUIRED:
        return ActionReconciliationRequiredEventResourceV2(
            event=event.event_kind.value,
            request_base64=encode_canonical_base64(event.payload),
            request_hash=event.payload_hash.value,
            **common,
        )
    if event.event_kind is RunEventKind.ACTION_RECONCILIATION_RESOLVED:
        if projection.receipt is None:
            raise ValueError("resolved V2 event has no receipt")
        return ActionReconciliationResolvedEventResourceV2(
            event=event.event_kind.value,
            receipt=receipt_resource(projection.receipt),
            **common,
        )
    if event.event_kind is RunEventKind.ACTION_COMPLETED:
        if projection.receipt is None:
            raise ValueError("completed V2 Action event has no receipt")
        return ActionCompletedEventResourceV2(
            event=event.event_kind.value,
            receipt=receipt_resource(projection.receipt),
            **common,
        )
    if event.event_kind is RunEventKind.WAITING_INPUT:
        return WaitingInputEventResourceV2(
            event=event.event_kind.value, answer_type="integer", **common
        )
    if event.event_kind is RunEventKind.WAIT_ANSWERED:
        if not is_canonical_integer_bytes(event.payload):
            raise ValueError("durable V2 wait answer is not canonical integer text")
        return WaitAnsweredEventResourceV2(
            event=event.event_kind.value,
            answer=event.payload.decode("ascii"),
            answer_hash=event.payload_hash.value,
            **common,
        )
    if event.event_kind is RunEventKind.SUBWORKFLOW_COMPLETED:
        if not is_canonical_integer_bytes(event.payload):
            raise ValueError(
                "durable V2 subworkflow result is not canonical integer text"
            )
        return SubworkflowCompletedEventResourceV2(
            event=event.event_kind.value,
            result=int(event.payload.decode("ascii")),
            result_hash=event.payload_hash.value,
            **common,
        )
    raise AssertionError("closed V2 event union was extended without API mapping")
