"""The schemas the queue's read and decision doors answer with.

One durable queue row travels as a page item, as the answer to a proposal, and
as the answer to an admission, so the shapes live together rather than beside
whichever door was written first.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from atelier2.api.references import (
    MAX_SIGNED_INT64,
    REVISION_HASH_PATTERN,
    SHA256_HASH_PATTERN,
)
from atelier2.api.wire.resources import ApiModel
from atelier2.contracts.host_configuration import MAXIMUM_PROJECT_ID_CHARACTERS
from atelier2.contracts.queue_projection import (
    MAXIMUM_QUEUE_ACTIVE_RUNS,
    MAXIMUM_QUEUE_ADMISSION_RATIONALE_CHARACTERS,
    MAXIMUM_QUEUE_AUTOMATION_LABEL_CHARACTERS,
    MAXIMUM_QUEUE_ITEM_TITLE_CHARACTERS,
    MAXIMUM_TRACKER_ITEM_REFERENCE_CHARACTERS,
    QueueAutomationDisposition,
    QueueBlockerKind,
    QueueDecisionAuthority,
    QueueItemState,
    QueueProposalSource,
)
from atelier2.contracts.when import RECORDED_AT_PATTERN


class QueuePriorityRankResource(ApiModel):
    rank: int = Field(ge=1, le=MAX_SIGNED_INT64)


class QueueProposalResource(ApiModel):
    revision: int = Field(ge=1, le=MAX_SIGNED_INT64)
    priority: QueuePriorityRankResource
    workflow_lineage_id: str = Field(pattern=SHA256_HASH_PATTERN)
    prerequisite_item_ids: tuple[str, ...]
    automation_disposition: QueueAutomationDisposition
    policy_revision: int | None = Field(default=None, ge=1, le=MAX_SIGNED_INT64)
    source: QueueProposalSource


class QueueAdmissionResource(ApiModel):
    proposal_revision: int | None = Field(default=None, ge=1, le=MAX_SIGNED_INT64)
    authority: QueueDecisionAuthority | None
    rationale: str = Field(
        min_length=1, max_length=MAXIMUM_QUEUE_ADMISSION_RATIONALE_CHARACTERS
    )


class QueueLaunchBindingResource(ApiModel):
    proposal_revision: int = Field(ge=1, le=MAX_SIGNED_INT64)
    run_id: str = Field(min_length=1)
    workflow_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)


class QueueItemResource(ApiModel):
    """One exhaustive durable queue row; tracker display enrichment is explicit."""

    project_id: str = Field(min_length=1, max_length=MAXIMUM_PROJECT_ID_CHARACTERS)
    tracker_item_reference: str = Field(
        min_length=1, max_length=MAXIMUM_TRACKER_ITEM_REFERENCE_CHARACTERS
    )
    item_id: str = Field(pattern=SHA256_HASH_PATTERN)
    state: QueueItemState
    revision: int = Field(ge=0, le=MAX_SIGNED_INT64)
    proposal: QueueProposalResource | None
    admission: QueueAdmissionResource | None
    launch_binding: QueueLaunchBindingResource | None
    blockers: tuple[QueueBlockerKind, ...]
    tracker_enrichment: Literal["ENRICHMENT_UNAVAILABLE"]
    title: str | None = Field(
        min_length=1,
        max_length=MAXIMUM_QUEUE_ITEM_TITLE_CHARACTERS,
        description=(
            "The tracker title last observed at title_observed_at; an observation, "
            "not current truth."
        ),
    )
    title_observed_at: str | None = Field(
        pattern=RECORDED_AT_PATTERN,
        description="When the tracker title was last observed.",
    )
    retired_at: str | None = Field(
        pattern=RECORDED_AT_PATTERN,
        description="When import observed the item absent from the tracker's open set.",
    )


class QueueItemPageResource(ApiModel):
    items: tuple[QueueItemResource, ...]
    next_after: str | None = Field(pattern=SHA256_HASH_PATTERN)


class QueueProjectPolicyResource(ApiModel):
    project_id: str = Field(min_length=1, max_length=MAXIMUM_PROJECT_ID_CHARACTERS)
    revision_number: int = Field(ge=1, le=MAX_SIGNED_INT64)
    maximum_active_runs: int = Field(ge=1, le=MAXIMUM_QUEUE_ACTIVE_RUNS)
    automation_label: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAXIMUM_QUEUE_AUTOMATION_LABEL_CHARACTERS,
    )
    default_workflow_lineage_id: str | None = Field(
        default=None, pattern=SHA256_HASH_PATTERN
    )
    default_priority_rank: int | None = Field(default=None, ge=1, le=MAX_SIGNED_INT64)
    automation_disposition_default: QueueAutomationDisposition | None = None


class QueueProposalDecisionResource(ApiModel):
    item_id: str = Field(pattern=SHA256_HASH_PATTERN)
    state: Literal[QueueItemState.PROPOSED]
    revision: int = Field(ge=1, le=MAX_SIGNED_INT64)
    proposal: QueueProposalResource


class QueueAdmissionDecisionResource(ApiModel):
    item_id: str = Field(pattern=SHA256_HASH_PATTERN)
    state: Literal[QueueItemState.ADMITTED]
    revision: int = Field(ge=1, le=MAX_SIGNED_INT64)
    admission: QueueAdmissionResource
