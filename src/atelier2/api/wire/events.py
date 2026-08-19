"""The schemas an event stream frames, in each workflow format version."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from atelier2.api.references import (
    EVENT_CURSOR_PATTERN,
    MAX_SIGNED_INT64,
    PUBLIC_RUN_REFERENCE_PATTERN,
    REVISION_HASH_PATTERN,
    SHA256_HASH_PATTERN,
)
from atelier2.api.wire.resources import (
    ApiModel,
    CancellationDispositionName,
    EffectReceiptResource,
    NodeRailResource,
)
from atelier2.contracts.agents import MAXIMUM_AGENT_FIELD_CHARACTERS


class RunEventBaseResource(ApiModel):
    cursor: str = Field(pattern=EVENT_CURSOR_PATTERN)
    sequence: int = Field(ge=1, le=MAX_SIGNED_INT64)
    public_run_reference: str = Field(pattern=PUBLIC_RUN_REFERENCE_PATTERN)
    workflow_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)
    node_id: str = Field(min_length=1)
    node_execution_id: str = Field(pattern=SHA256_HASH_PATTERN)
    event_hash: str = Field(pattern=SHA256_HASH_PATTERN)


class AgentCompletedEventResource(RunEventBaseResource):
    event: Literal["AGENT_COMPLETED"]
    output: str
    payload_hash: str = Field(pattern=SHA256_HASH_PATTERN)


class ActionReconciliationRequiredEventResource(RunEventBaseResource):
    event: Literal["ACTION_RECONCILIATION_REQUIRED"]
    request_base64: str
    request_hash: str = Field(pattern=SHA256_HASH_PATTERN)


class ActionReconciliationResolvedEventResource(RunEventBaseResource):
    event: Literal["ACTION_RECONCILIATION_RESOLVED"]
    receipt: EffectReceiptResource


class ActionCompletedEventResource(RunEventBaseResource):
    event: Literal["ACTION_COMPLETED"]
    receipt: EffectReceiptResource


class WaitingInputEventResource(RunEventBaseResource):
    event: Literal["WAITING_INPUT"]
    answer_type: Literal["integer"]


class WaitAnsweredEventResource(RunEventBaseResource):
    event: Literal["WAIT_ANSWERED"]
    answer: str
    answer_hash: str = Field(pattern=SHA256_HASH_PATTERN)


class SubworkflowCompletedEventResource(RunEventBaseResource):
    event: Literal["SUBWORKFLOW_COMPLETED"]
    result: int
    result_hash: str = Field(pattern=SHA256_HASH_PATTERN)


RunEventResource = Annotated[
    AgentCompletedEventResource
    | ActionReconciliationRequiredEventResource
    | ActionReconciliationResolvedEventResource
    | ActionCompletedEventResource
    | WaitingInputEventResource
    | WaitAnsweredEventResource
    | SubworkflowCompletedEventResource,
    Field(discriminator="event"),
]


class RunEventBaseResourceV2(ApiModel):
    workflow_format_version: Literal[2]
    cursor: str = Field(pattern=EVENT_CURSOR_PATTERN)
    sequence: int = Field(ge=1, le=MAX_SIGNED_INT64)
    public_run_reference: str = Field(pattern=PUBLIC_RUN_REFERENCE_PATTERN)
    workflow_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)
    node_id: str = Field(min_length=1)
    node_execution_id: str = Field(pattern=SHA256_HASH_PATTERN)
    event_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    node_rail: tuple[NodeRailResource, ...] = Field(min_length=1)
    """Where the whole run stands once this event is folded into the snapshot.

    A reader of one event needs the run, not the node: a finished node hands its
    successor work, and answering that from the event alone is the derivation
    this rail exists to end. V1 events cannot carry it — their resource is
    byte-frozen — so a V1 cockpit keeps deriving until the V3 cutover (#63).
    """


class AgentCompletedEventResourceV2(RunEventBaseResourceV2):
    event: Literal["AGENT_COMPLETED"]
    output_base64: str
    output_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    attempt_id: str = Field(pattern=SHA256_HASH_PATTERN)
    attempt_ordinal: Literal[1, 2]


class AgentFailedEventResourceV2(RunEventBaseResourceV2):
    event: Literal["AGENT_FAILED"]
    failure_code: Literal["PROCESS_EXITED_UNSUCCESSFULLY"]
    attempt_id: str = Field(pattern=SHA256_HASH_PATTERN)
    attempt_ordinal: Literal[1, 2]


class AgentCancelRequestedEventResourceV2(RunEventBaseResourceV2):
    event: Literal["AGENT_CANCEL_REQUESTED"]
    attempt_id: str = Field(pattern=SHA256_HASH_PATTERN)
    attempt_ordinal: Literal[1, 2]
    command_id: str = Field(min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS)
    replacement: Literal["NONE", "ONE"]


class AgentCancelledEventResourceV2(RunEventBaseResourceV2):
    event: Literal["AGENT_CANCELLED"]
    attempt_id: str = Field(pattern=SHA256_HASH_PATTERN)
    attempt_ordinal: Literal[1, 2]
    command_id: str = Field(min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS)
    replacement: Literal["NONE", "ONE"]
    disposition: CancellationDispositionName
    replacement_attempt_id: str | None = Field(pattern=SHA256_HASH_PATTERN)


class AgentInterruptedEventResourceV2(RunEventBaseResourceV2):
    event: Literal["AGENT_INTERRUPTED"]
    attempt_id: str = Field(pattern=SHA256_HASH_PATTERN)
    attempt_ordinal: Literal[1, 2]
    command_id: str = Field(min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS)
    replacement: Literal["NONE", "ONE"]
    disposition: CancellationDispositionName
    replacement_attempt_id: str | None = Field(pattern=SHA256_HASH_PATTERN)


class ActionReconciliationRequiredEventResourceV2(RunEventBaseResourceV2):
    event: Literal["ACTION_RECONCILIATION_REQUIRED"]
    request_base64: str
    request_hash: str = Field(pattern=SHA256_HASH_PATTERN)


class ActionReconciliationResolvedEventResourceV2(RunEventBaseResourceV2):
    event: Literal["ACTION_RECONCILIATION_RESOLVED"]
    receipt: EffectReceiptResource


class ActionCompletedEventResourceV2(RunEventBaseResourceV2):
    event: Literal["ACTION_COMPLETED"]
    receipt: EffectReceiptResource


class WaitingInputEventResourceV2(RunEventBaseResourceV2):
    event: Literal["WAITING_INPUT"]
    answer_type: Literal["integer"]


class WaitAnsweredEventResourceV2(RunEventBaseResourceV2):
    event: Literal["WAIT_ANSWERED"]
    answer: str
    answer_hash: str = Field(pattern=SHA256_HASH_PATTERN)


class SubworkflowCompletedEventResourceV2(RunEventBaseResourceV2):
    event: Literal["SUBWORKFLOW_COMPLETED"]
    result: int
    result_hash: str = Field(pattern=SHA256_HASH_PATTERN)


RunEventResourceV2 = Annotated[
    AgentCompletedEventResourceV2
    | AgentFailedEventResourceV2
    | AgentCancelRequestedEventResourceV2
    | AgentCancelledEventResourceV2
    | AgentInterruptedEventResourceV2
    | ActionReconciliationRequiredEventResourceV2
    | ActionReconciliationResolvedEventResourceV2
    | ActionCompletedEventResourceV2
    | WaitingInputEventResourceV2
    | WaitAnsweredEventResourceV2
    | SubworkflowCompletedEventResourceV2,
    Field(discriminator="event"),
]


class RunEventBaseResourceV3(ApiModel):
    workflow_format_version: Literal[3]
    cursor: str = Field(pattern=EVENT_CURSOR_PATTERN)
    sequence: int = Field(ge=1, le=MAX_SIGNED_INT64)
    public_run_reference: str = Field(pattern=PUBLIC_RUN_REFERENCE_PATTERN)
    workflow_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)
    node_id: str = Field(min_length=1)
    node_execution_id: str = Field(pattern=SHA256_HASH_PATTERN)
    event_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    node_rail: tuple[NodeRailResource, ...] = Field(min_length=1)
    """Where the whole run stands once this event is folded into the snapshot.

    A format-3 line writes agent events through the same attempt store as V2 and
    its pauses through the same wait path, so the rail travels with all of them
    the same way. V1 stays byte-frozen and cannot carry it. Action and
    Subworkflow V3 resources are not invented here: no format-3 run persists
    those kinds today.
    """


class AgentCompletedEventResourceV3(RunEventBaseResourceV3):
    event: Literal["AGENT_COMPLETED"]
    output_base64: str
    output_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    attempt_id: str = Field(pattern=SHA256_HASH_PATTERN)
    attempt_ordinal: Literal[1, 2]


class AgentFailedEventResourceV3(RunEventBaseResourceV3):
    """A V3 agent attempt ended, with the stored receipt words when they exist.

    `failure_code` is the attempt's closed name. `reason` is the same
    `node-receipt/v3` sentence the store already kept — not a second
    vocabulary, and none when no receipt was written.
    """

    event: Literal["AGENT_FAILED"]
    failure_code: Literal[
        "PROCESS_EXITED_UNSUCCESSFULLY",
        "OUTPUT_SCHEMA_REFUSED",
        "AGENT_REFUSED",
    ]
    reason: str | None
    attempt_id: str = Field(pattern=SHA256_HASH_PATTERN)
    attempt_ordinal: Literal[1, 2]


class AgentCancelRequestedEventResourceV3(RunEventBaseResourceV3):
    event: Literal["AGENT_CANCEL_REQUESTED"]
    attempt_id: str = Field(pattern=SHA256_HASH_PATTERN)
    attempt_ordinal: Literal[1, 2]
    command_id: str = Field(min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS)
    replacement: Literal["NONE", "ONE"]


class AgentCancelledEventResourceV3(RunEventBaseResourceV3):
    event: Literal["AGENT_CANCELLED"]
    attempt_id: str = Field(pattern=SHA256_HASH_PATTERN)
    attempt_ordinal: Literal[1, 2]
    command_id: str = Field(min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS)
    replacement: Literal["NONE", "ONE"]
    disposition: CancellationDispositionName
    replacement_attempt_id: str | None = Field(pattern=SHA256_HASH_PATTERN)


class AgentInterruptedEventResourceV3(RunEventBaseResourceV3):
    event: Literal["AGENT_INTERRUPTED"]
    attempt_id: str = Field(pattern=SHA256_HASH_PATTERN)
    attempt_ordinal: Literal[1, 2]
    command_id: str = Field(min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS)
    replacement: Literal["NONE", "ONE"]
    disposition: CancellationDispositionName
    replacement_attempt_id: str | None = Field(pattern=SHA256_HASH_PATTERN)


class WaitingInputEventResourceV3(RunEventBaseResourceV3):
    """A format-3 run has stopped at a Wait node and is owed an answer.

    It carries no `answer_type`. The V1 and V2 shapes name one because their Wait
    node declares one; a V3 Wait node declares an output with a schema instead, so
    the honest answer to "what may I send" is the published schema that node
    pinned, reachable from the workflow revision this event names -- and stating
    `integer` here would be a shape nothing enforces.
    """

    event: Literal["WAITING_INPUT"]


class WaitAnsweredEventResourceV3(RunEventBaseResourceV3):
    """The exact bytes a person answered with, and the hash the run kept.

    Base64 rather than the V2 shape's decimal text, for the same reason an agent
    output travels that way: a V3 answer is whatever JSON value its schema
    admits, and rendering it as a number would be a lie for every other value.
    """

    event: Literal["WAIT_ANSWERED"]
    answer_base64: str
    answer_hash: str = Field(pattern=SHA256_HASH_PATTERN)


RunEventResourceV3 = Annotated[
    AgentCompletedEventResourceV3
    | AgentFailedEventResourceV3
    | AgentCancelRequestedEventResourceV3
    | AgentCancelledEventResourceV3
    | AgentInterruptedEventResourceV3
    | WaitingInputEventResourceV3
    | WaitAnsweredEventResourceV3,
    Field(discriminator="event"),
]

AnyRunEventResource = RunEventResource | RunEventResourceV2 | RunEventResourceV3
