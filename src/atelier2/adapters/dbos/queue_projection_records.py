"""How a queue policy revision and a queue proposal are spelled as durable rows.

The store owns the engine, the transaction and every query; this module owns
nothing but the translation between one `queue_project_policy_revisions` or
`queue_proposal_revisions` row and the contract it stands for. Both directions
live in one module, so a column the writer spells one way and the reader
another cannot hide in the distance between two files.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from atelier2.contracts.catalog_v3 import CatalogLineageId
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.queue_projection import (
    QueueAutomationDisposition,
    QueueItemId,
    QueuePriorityRank,
    QueueProjectPolicyDefaults,
    QueueProjectPolicyRevision,
    QueueProposal,
    QueueProposalSource,
    WorkItemReference,
)

type _RowValue = str | int | None


def policy_from_record(record: Mapping[Any, Any]) -> QueueProjectPolicyRevision:
    """The policy one durable revision row states.

    The three default columns stand or fall together: a row carrying some of
    them describes a proposal nobody could write, so it is corrupt state rather
    than a policy with a gap.
    """

    lineage_id = record["default_workflow_lineage_id"]
    priority_rank = record["default_priority_rank"]
    disposition = record["automation_disposition_default"]
    if (lineage_id is None) != (priority_rank is None) or (lineage_id is None) != (
        disposition is None
    ):
        raise ValueError("a queue policy states all of its proposal defaults or none")
    return QueueProjectPolicyRevision(
        ProjectId(str(record["project_id"])),
        int(record["revision_number"]),
        int(record["maximum_active_runs"]),
        (
            None
            if record["automation_label"] is None
            else str(record["automation_label"])
        ),
        (
            None
            if lineage_id is None
            else QueueProjectPolicyDefaults(
                CatalogLineageId(str(lineage_id)),
                QueuePriorityRank(int(priority_rank)),
                QueueAutomationDisposition(str(disposition)),
            )
        ),
    )


def policy_row(policy: QueueProjectPolicyRevision) -> dict[str, _RowValue]:
    """The durable revision row one policy is published as."""

    defaults = policy.defaults
    return {
        "project_id": policy.project_id.value,
        "revision_number": policy.revision_number,
        "maximum_active_runs": policy.maximum_active_runs,
        "automation_label": policy.automation_label,
        "default_workflow_lineage_id": (
            None if defaults is None else defaults.workflow_lineage_id.value
        ),
        "default_priority_rank": (None if defaults is None else defaults.priority.rank),
        "automation_disposition_default": (
            None if defaults is None else defaults.automation_disposition.value
        ),
    }


def proposal_from_record(
    record: Mapping[Any, Any], prerequisite_item_ids: tuple[QueueItemId, ...]
) -> QueueProposal:
    """The proposal one durable revision row and its dependency edges state."""

    return QueueProposal(
        QueuePriorityRank(int(record["priority_rank"])),
        CatalogLineageId(str(record["workflow_lineage_id"])),
        prerequisite_item_ids,
        QueueAutomationDisposition(str(record["automation_disposition"])),
        (None if record["policy_revision"] is None else int(record["policy_revision"])),
        QueueProposalSource(str(record["source"])),
    )


def proposal_row(
    item_reference: WorkItemReference, revision: int, proposal: QueueProposal
) -> dict[str, _RowValue]:
    """The durable revision row one proposal is written as, edges excluded."""

    return {
        "item_id": item_reference.item_id.value,
        "proposal_revision": revision,
        "project_id": item_reference.project.value,
        "priority_rank": proposal.priority.rank,
        "workflow_lineage_id": proposal.workflow_lineage_id.value,
        "automation_disposition": proposal.automation_disposition.value,
        "policy_revision": proposal.policy_revision,
        "source": proposal.source.value,
    }
