"""The schemas an event stream frames, in both workflow format versions."""

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
    disposition: Literal[
        "NEVER_LAUNCHED",
        "EXITED_BEFORE_SIGNAL",
        "REAPED_AFTER_TERM",
        "REAPED_AFTER_KILL",
        "OWNER_LOST_AFTER_PARENT_DEATH",
    ]
    replacement_attempt_id: str | None = Field(pattern=SHA256_HASH_PATTERN)


class AgentInterruptedEventResourceV2(RunEventBaseResourceV2):
    event: Literal["AGENT_INTERRUPTED"]
    attempt_id: str = Field(pattern=SHA256_HASH_PATTERN)
    attempt_ordinal: Literal[1, 2]
    command_id: str = Field(min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS)
    replacement: Literal["NONE", "ONE"]
    disposition: Literal[
        "NEVER_LAUNCHED",
        "EXITED_BEFORE_SIGNAL",
        "REAPED_AFTER_TERM",
        "REAPED_AFTER_KILL",
        "OWNER_LOST_AFTER_PARENT_DEATH",
    ]
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


class RunEventBaseResourceV3(ApiModel):
    """One durable event of a format-3 run, as its own shape.

    A V3 run used to project through the V1 mapping, which says nothing about the
    format and drops the rail: a cockpit reading the stream of a run it started
    could not tell which family it was looking at. The five kinds below are every
    kind a V3 run can write -- its nodes are agents, so an Action, a Wait or a
    subworkflow completion cannot occur and is refused rather than rendered.
    """

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

    Same promise as the V2 shape makes, and a V3 reader needs it more: a V3 line
    ends on its agent sink, so the event that ends the run and the event that
    hands work on are the same kind, and only the rail says which happened.
    """


class AgentCompletedEventResourceV3(RunEventBaseResourceV3):
    event: Literal["AGENT_COMPLETED"]
    output_base64: str
    output_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    attempt_id: str = Field(pattern=SHA256_HASH_PATTERN)
    attempt_ordinal: Literal[1, 2]


class AgentFailedEventResourceV3(RunEventBaseResourceV3):
    event: Literal["AGENT_FAILED"]
    failure_code: Literal["PROCESS_EXITED_UNSUCCESSFULLY"]
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
    disposition: Literal[
        "NEVER_LAUNCHED",
        "EXITED_BEFORE_SIGNAL",
        "REAPED_AFTER_TERM",
        "REAPED_AFTER_KILL",
        "OWNER_LOST_AFTER_PARENT_DEATH",
    ]
    replacement_attempt_id: str | None = Field(pattern=SHA256_HASH_PATTERN)


class AgentInterruptedEventResourceV3(RunEventBaseResourceV3):
    event: Literal["AGENT_INTERRUPTED"]
    attempt_id: str = Field(pattern=SHA256_HASH_PATTERN)
    attempt_ordinal: Literal[1, 2]
    command_id: str = Field(min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS)
    replacement: Literal["NONE", "ONE"]
    disposition: Literal[
        "NEVER_LAUNCHED",
        "EXITED_BEFORE_SIGNAL",
        "REAPED_AFTER_TERM",
        "REAPED_AFTER_KILL",
        "OWNER_LOST_AFTER_PARENT_DEATH",
    ]
    replacement_attempt_id: str | None = Field(pattern=SHA256_HASH_PATTERN)


RunEventResourceV3 = Annotated[
    AgentCompletedEventResourceV3
    | AgentFailedEventResourceV3
    | AgentCancelRequestedEventResourceV3
    | AgentCancelledEventResourceV3
    | AgentInterruptedEventResourceV3,
    Field(discriminator="event"),
]

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

AnyRunEventResource = RunEventResource | RunEventResourceV2 | RunEventResourceV3
