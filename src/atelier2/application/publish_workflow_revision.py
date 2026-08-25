from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.read_workflow_revisions import (
    WorkflowRevisionRead,
    describe_workflow_revision,
)
from atelier2.application.refusals import (
    DurableStateCorrupt,
    ReadUnavailable,
    WriteUnavailable,
)
from atelier2.contracts.runs import WorkflowRevision
from atelier2.contracts.workflow_projections import (
    WorkflowRevisionProjection,
)
from atelier2.contracts.workflow_refusals import (
    WorkflowDocumentInvalid,
    WorkflowRefusal,
)
from atelier2.contracts.workflows import AgentNode, AgentNodeV2, SubworkflowNode
from atelier2.contracts.workflows_v3 import AnyWorkflowDocument, WorkflowGraphV3
from atelier2.ports.durable_runs import (
    DurableStateCorrupt as PortDurableStateCorrupt,
)
from atelier2.ports.durable_runs import (
    DurableWriteUnavailable,
)
from atelier2.ports.published_revisions import PublishedRevisionResolver
from atelier2.ports.workflow_revisions import (
    DurableRevisionCollision,
    DurableRevisionCreated,
    DurableRevisionExisting,
    ProjectionLimitExceeded,
    WorkflowDocumentParser,
    WorkflowRevisionPublisher,
)


@dataclass(frozen=True)
class WorkflowPublicationLimits:
    maximum_document_bytes: int
    maximum_nodes: int
    maximum_string_characters: int
    maximum_payload_bytes: int

    def __post_init__(self) -> None:
        for name, value in self.__dict__.items():
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")

    def validate(self, document: bytes, graph: AnyWorkflowDocument) -> None:
        self.validate_document(document)
        self.validate_graph(graph)

    def validate_document(self, document: bytes) -> None:
        self.validate_document_length(len(document))

    @property
    def maximum_field_characters(self) -> int:
        return self.maximum_string_characters

    def validate_document_length(self, byte_count: int) -> None:
        if byte_count > self.maximum_document_bytes:
            raise ProjectionLimitExceeded(
                "workflow document exceeds its encoded response limit"
            )

    def validate_field_length(self, character_count: int) -> None:
        if character_count > self.maximum_string_characters:
            raise ProjectionLimitExceeded(
                "durable text exceeds its response character limit"
            )

    def validate_payload_length(self, byte_count: int) -> None:
        if byte_count > self.maximum_payload_bytes:
            raise ProjectionLimitExceeded(
                "durable payload exceeds its encoded response limit"
            )

    def validate_graph(self, graph: AnyWorkflowDocument) -> None:
        if len(graph.nodes) > self.maximum_nodes:
            raise ProjectionLimitExceeded("workflow exceeds its node limit")
        if any(
            len(value) > self.maximum_string_characters
            for value in _projected_strings(graph)
        ):
            raise ProjectionLimitExceeded("workflow string exceeds its character limit")


def _projected_strings(graph: AnyWorkflowDocument) -> tuple[str, ...]:
    """Every string the revision projection renders; a V3 projection renders none."""
    if isinstance(graph, WorkflowGraphV3):
        return ()
    values = [graph.start]
    for node in graph.nodes:
        values.append(node.id)
        if isinstance(node, AgentNode):
            values.extend((node.job, node.output))
        if isinstance(node, AgentNodeV2):
            values.extend((node.role, node.job))
        if not isinstance(node, SubworkflowNode):
            values.append(node.next)
    return tuple(values)


@dataclass(frozen=True)
class PublicationCreated:
    """The stored revision, answered exactly as a read of it answers."""

    read: WorkflowRevisionRead


@dataclass(frozen=True)
class PublicationExisting:
    read: WorkflowRevisionRead


@dataclass(frozen=True)
class PublicationInvalid:
    detail: str
    refusal: WorkflowRefusal | None = None


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


def publish_workflow_revision(
    document: bytes,
    publisher: WorkflowRevisionPublisher,
    parser: WorkflowDocumentParser,
    limits: WorkflowPublicationLimits,
    resolver: PublishedRevisionResolver,
) -> PublishWorkflowRevisionResult:
    """Store one document and answer what a read of the stored revision answers.

    The resolver is here because whether this build runs the document is part
    of that answer, and half of it -- does every pinned reference resolve -- is
    not in the bytes. One describer serves the publication and the read, so the
    two cannot disagree about the revision just written.
    """
    try:
        revision = WorkflowRevision(document)
        graph = parser(document)
        limits.validate(document, graph)
    except WorkflowDocumentInvalid as refused:
        return PublicationInvalid(str(refused), refused.refusal)
    except (TypeError, ValueError) as error:
        return PublicationInvalid(str(error))
    result = publisher.publish(revision)
    match result:
        case DurableRevisionCreated(stored):
            return _answered(
                PublicationCreated,
                describe_workflow_revision(
                    WorkflowRevisionProjection(stored, graph), resolver
                ),
            )
        case DurableRevisionExisting(stored):
            return _answered(
                PublicationExisting,
                describe_workflow_revision(
                    WorkflowRevisionProjection(stored, graph), resolver
                ),
            )
        case DurableRevisionCollision():
            return PublicationCollision()
        case DurableWriteUnavailable():
            return WriteUnavailable()
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)


def _answered(
    outcome: type[PublicationCreated | PublicationExisting],
    described: WorkflowRevisionRead | ReadUnavailable | DurableStateCorrupt,
) -> PublishWorkflowRevisionResult:
    """The stored revision described, or why it could not be after the write.

    The revision is durable either way; a registry that would not answer for
    the references it pins is a later attempt's problem, and a retry answers
    `PublicationExisting` with the description it could not give now.
    """
    match described:
        case WorkflowRevisionRead():
            return outcome(described)
        case ReadUnavailable(detail):
            return WriteUnavailable(detail)
        case DurableStateCorrupt():
            return described
        case _ as unreachable:
            assert_never(unreachable)
