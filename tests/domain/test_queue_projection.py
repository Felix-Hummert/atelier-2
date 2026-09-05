from __future__ import annotations

import pytest

from atelier2.contracts.catalog_v3 import CatalogLineageId
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.queue_projection import (
    QUEUE_AUTOMATION_LABEL_WILDCARD,
    ConfirmQueueProposal,
    QueueAdmission,
    QueueAdmissionProposalRequired,
    QueueAdmissionRationale,
    QueueAutomationDisposition,
    QueueDecisionAuthority,
    QueueItemSnapshot,
    QueueItemState,
    QueuePriorityRank,
    QueueProjectionRevision,
    QueueProjectPolicyDefaults,
    QueueProjectPolicyRevision,
    QueueProposal,
    TrackerItemReference,
    WorkItemReference,
)

REFERENCE = WorkItemReference(ProjectId("project1"), TrackerItemReference("gh:79"))
LINEAGE = CatalogLineageId("a1" * 32)
OTHER_LINEAGE = CatalogLineageId("b2" * 32)
PROPOSAL = QueueProposal(
    QueuePriorityRank(1),
    LINEAGE,
    (),
    QueueAutomationDisposition.HUMAN_REQUIRED,
    1,
)


@pytest.mark.parametrize(
    ("revision", "admission"),
    [
        (
            QueueProjectionRevision(2),
            QueueAdmission(LINEAGE, QueueAdmissionRationale("legacy-shaped")),
        ),
        (
            QueueProjectionRevision(2),
            QueueAdmission(
                OTHER_LINEAGE,
                QueueAdmissionRationale("wrong lineage"),
                QueueDecisionAuthority.OPERATOR,
                QueueProjectionRevision(1),
            ),
        ),
        (
            QueueProjectionRevision(9),
            QueueAdmission(
                LINEAGE,
                QueueAdmissionRationale("wrong revision"),
                QueueDecisionAuthority.OPERATOR,
                QueueProjectionRevision(1),
            ),
        ),
    ],
)
def test_admitted_proposal_requires_its_exact_typed_admission(
    revision: QueueProjectionRevision,
    admission: QueueAdmission,
) -> None:
    with pytest.raises(ValueError, match="exact authority and revision"):
        QueueItemSnapshot(
            REFERENCE,
            QueueItemState.ADMITTED,
            revision,
            admission,
            PROPOSAL,
        )


@pytest.mark.parametrize(
    ("state", "revision", "proposal", "message"),
    [
        (
            QueueItemState.OBSERVED,
            QueueProjectionRevision(1),
            None,
            "revision zero",
        ),
        (
            QueueItemState.PROPOSED,
            QueueProjectionRevision(0),
            PROPOSAL,
            "positive revision",
        ),
    ],
)
def test_queue_lifecycle_state_requires_its_exact_revision_shape(
    state: QueueItemState,
    revision: QueueProjectionRevision,
    proposal: QueueProposal | None,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        QueueItemSnapshot(REFERENCE, state, revision, None, proposal)


def test_observed_item_requires_a_proposal_before_confirmation() -> None:
    snapshot = QueueItemSnapshot(
        REFERENCE,
        QueueItemState.OBSERVED,
        QueueProjectionRevision(0),
        None,
    )
    command = ConfirmQueueProposal(
        REFERENCE,
        QueueProjectionRevision(0),
        QueueAdmissionRationale("cannot skip proposal"),
    )

    assert snapshot.confirm(command) == QueueAdmissionProposalRequired(
        REFERENCE, QueueItemState.OBSERVED
    )


def test_a_policy_names_one_automation_label_and_refuses_the_wildcard() -> None:
    """ "All" is a decision the operator has not made (#79 ruling 1)."""

    project = REFERENCE.project

    assert (
        QueueProjectPolicyRevision(project, 1, 2, "bereit").automation_label == "bereit"
    )
    with pytest.raises(ValueError, match="names one label"):
        QueueProjectPolicyRevision(project, 1, 2, QUEUE_AUTOMATION_LABEL_WILDCARD)


def test_a_policy_default_reserves_its_work_for_a_human_unless_stated() -> None:
    """REQ-QUEUE-05: the queue proposes the work; only a stated word releases it."""

    unstated = QueueProjectPolicyDefaults(LINEAGE, QueuePriorityRank(2))
    stated = QueueProjectPolicyDefaults(
        LINEAGE,
        QueuePriorityRank(2),
        QueueAutomationDisposition.AUTOMATION_AUTHORIZED,
    )

    assert unstated.automation_disposition is QueueAutomationDisposition.HUMAN_REQUIRED
    assert (
        stated.automation_disposition
        is QueueAutomationDisposition.AUTOMATION_AUTHORIZED
    )
