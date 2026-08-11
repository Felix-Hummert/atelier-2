from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.contracts.runs import WorkflowRevision
from atelier2.contracts.workflows import AgentNode, SubworkflowNode, WorkflowGraph
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
    ProjectionLimitExceeded,
    WorkflowDocumentParser,
    WorkflowRevisionProjection,
    WorkflowRevisionPublisher,
)


@dataclass(frozen=True)
class WorkflowPublicationLimits:
    maximum_document_bytes: int
    maximum_nodes: int
    maximum_string_characters: int

    def __post_init__(self) -> None:
        for name, value in self.__dict__.items():
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")

    def validate(self, document: bytes, graph: WorkflowGraph) -> None:
        self.validate_document(document)
        self.validate_graph(graph)

    def validate_document(self, document: bytes) -> None:
        if len(document) > self.maximum_document_bytes:
            raise ProjectionLimitExceeded(
                "workflow document exceeds its encoded response limit"
            )

    def validate_graph(self, graph: WorkflowGraph) -> None:
        if len(graph.nodes) > self.maximum_nodes:
            raise ProjectionLimitExceeded("workflow exceeds its node limit")
        values = [graph.start]
        for node in graph.nodes:
            values.append(node.id)
            if isinstance(node, AgentNode):
                values.extend((node.job, node.output))
            if not isinstance(node, SubworkflowNode):
                values.append(node.next)
        if any(len(value) > self.maximum_string_characters for value in values):
            raise ProjectionLimitExceeded("workflow string exceeds its character limit")


@dataclass(frozen=True)
class PublicationCreated:
    projection: WorkflowRevisionProjection


@dataclass(frozen=True)
class PublicationExisting:
    projection: WorkflowRevisionProjection


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
    detail: str | None = None


@dataclass(frozen=True)
class DurableStateCorrupt:
    pass


def publish_workflow_revision(
    document: bytes,
    publisher: WorkflowRevisionPublisher,
    parser: WorkflowDocumentParser,
    limits: WorkflowPublicationLimits | None = None,
) -> PublishWorkflowRevisionResult:
    try:
        revision = WorkflowRevision(document)
        graph = parser(document)
        if limits is not None:
            limits.validate(document, graph)
    except (TypeError, ValueError) as error:
        return PublicationInvalid(str(error))
    result = publisher.publish(revision)
    match result:
        case DurableRevisionCreated(stored):
            return PublicationCreated(WorkflowRevisionProjection(stored, graph))
        case DurableRevisionExisting(stored):
            return PublicationExisting(WorkflowRevisionProjection(stored, graph))
        case DurableRevisionCollision():
            return PublicationCollision()
        case DurableWriteUnavailable():
            return WriteUnavailable()
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
