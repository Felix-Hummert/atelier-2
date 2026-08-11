from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.contracts.runs import WorkflowRevision
from atelier2.ports.durable_runs import (
    DurableStateCorrupt as PortDurableStateCorrupt,
)
from atelier2.ports.durable_runs import (
    DurableWriteUnavailable,
)
from atelier2.ports.workflow_revisions import (
    DurableRevisionCollision,
    DurableRevisionCreated,
    DurableRevisionExisting,
    WorkflowRevisionPublisher,
)


@dataclass(frozen=True)
class PublicationCreated:
    revision: WorkflowRevision


@dataclass(frozen=True)
class PublicationExisting:
    revision: WorkflowRevision


@dataclass(frozen=True)
class PublicationInvalid:
    detail: str


@dataclass(frozen=True)
class PublicationCollision:
    pass


type PublishWorkflowRevisionResult = (
    PublicationCreated
    | PublicationExisting
    | PublicationInvalid
    | PublicationCollision
    | WriteUnavailable
    | DurableStateCorrupt
)


@dataclass(frozen=True)
class WriteUnavailable:
    pass


@dataclass(frozen=True)
class DurableStateCorrupt:
    pass


def publish_workflow_revision(
    document: bytes, publisher: WorkflowRevisionPublisher
) -> PublishWorkflowRevisionResult:
    try:
        revision = WorkflowRevision(document)
        parse_workflow_document(document)
    except (TypeError, ValueError) as error:
        return PublicationInvalid(str(error))
    result = publisher.publish(revision)
    match result:
        case DurableRevisionCreated(stored):
            return PublicationCreated(stored)
        case DurableRevisionExisting(stored):
            return PublicationExisting(stored)
        case DurableRevisionCollision():
            return PublicationCollision()
        case DurableWriteUnavailable():
            return WriteUnavailable()
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
