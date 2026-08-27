"""Write the exact queue proposal and project policy an operator inspects."""

from __future__ import annotations

from dataclasses import dataclass

from atelier2.application.refusals import DurableStateCorrupt, WriteUnavailable
from atelier2.contracts.queue_projection import (
    PlanQueueItem,
    QueueProjectPolicyRevision,
    QueueProposalOutcome,
)
from atelier2.ports.durable_runs import DurableStateCorrupt as PortDurableStateCorrupt
from atelier2.ports.durable_runs import DurableWriteUnavailable
from atelier2.ports.queue_projection import (
    QueuePlanner,
    QueuePolicyWriter,
)
from atelier2.ports.queue_projection import (
    QueueProjectPolicyPublished as PortQueueProjectPolicyPublished,
)
from atelier2.ports.queue_projection import (
    QueueProjectPolicyRevisionConflict as PortQueueProjectPolicyRevisionConflict,
)
from atelier2.ports.queue_projection import (
    QueueProjectPolicyUnchanged as PortQueueProjectPolicyUnchanged,
)
from atelier2.ports.queue_projection import (
    QueueProposalRefused as PortQueueProposalRefused,
)


@dataclass(frozen=True)
class QueueProposalRefused:
    reason: str


@dataclass(frozen=True)
class QueueProjectPolicyPublished:
    policy: QueueProjectPolicyRevision


@dataclass(frozen=True)
class QueueProjectPolicyUnchanged:
    policy: QueueProjectPolicyRevision


@dataclass(frozen=True)
class QueueProjectPolicyRevisionConflict:
    expected_revision: int
    actual_revision: int


type PlanQueueItemOutcome = (
    QueueProposalOutcome | QueueProposalRefused | WriteUnavailable | DurableStateCorrupt
)
type PutQueueProjectPolicyOutcome = (
    QueueProjectPolicyPublished
    | QueueProjectPolicyUnchanged
    | QueueProjectPolicyRevisionConflict
    | WriteUnavailable
    | DurableStateCorrupt
)


def plan_queue_item(
    command: PlanQueueItem, queue: QueuePlanner
) -> PlanQueueItemOutcome:
    result = queue.plan(command)
    if isinstance(result, DurableWriteUnavailable):
        return WriteUnavailable()
    if isinstance(result, PortDurableStateCorrupt):
        return DurableStateCorrupt()
    if isinstance(result, PortQueueProposalRefused):
        return QueueProposalRefused(result.reason)
    return result


def put_queue_project_policy(
    policy: QueueProjectPolicyRevision,
    expected_revision: int,
    queue: QueuePolicyWriter,
) -> PutQueueProjectPolicyOutcome:
    result = queue.put_policy(policy, expected_revision)
    if isinstance(result, DurableWriteUnavailable):
        return WriteUnavailable()
    if isinstance(result, PortDurableStateCorrupt):
        return DurableStateCorrupt()
    if isinstance(result, PortQueueProjectPolicyPublished):
        return QueueProjectPolicyPublished(result.policy)
    if isinstance(result, PortQueueProjectPolicyUnchanged):
        return QueueProjectPolicyUnchanged(result.policy)
    if isinstance(result, PortQueueProjectPolicyRevisionConflict):
        return QueueProjectPolicyRevisionConflict(
            result.expected_revision, result.actual_revision
        )
    raise AssertionError("queue policy writer returned an unknown outcome")
