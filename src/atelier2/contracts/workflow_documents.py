"""Which formats a published workflow document may be written in.

`format_version` chooses the model one document is read against. The parser
dispatches on this table, and the API derives the shape it publishes from the
same entries -- so a format cannot be read without being described, and a field
cannot leave the model while the description still promises it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from pydantic import BaseModel

from atelier2.contracts.workflow_formats import WorkflowFormatVersion
from atelier2.contracts.workflows import WorkflowGraph, WorkflowGraphV2
from atelier2.contracts.workflows_v3 import (
    AnyWorkflowDocument,
    WorkflowGraphV3,
    validate_workflow_graph_v3,
)


@dataclass(frozen=True, slots=True)
class WorkflowDocumentFormat:
    """One document format: the model that decides it, and the call that reads it.

    Both halves are the same grammar seen twice. `read` is what a publication
    runs, `model` is what a description is derived from; keeping them in one
    record is what stops a reader and a description from drifting apart.
    """

    model: type[BaseModel]
    read: Callable[[Mapping[str, object]], AnyWorkflowDocument]


def _strictly[GraphT: BaseModel](
    model: type[GraphT],
) -> Callable[[Mapping[str, object]], GraphT]:
    """Read one document against a model that carries every rule itself."""

    def read(document: Mapping[str, object]) -> GraphT:
        return model.model_validate(document, strict=True)

    return read


WORKFLOW_DOCUMENT_FORMATS: Mapping[WorkflowFormatVersion, WorkflowDocumentFormat] = {
    WorkflowFormatVersion.V1: WorkflowDocumentFormat(
        WorkflowGraph, _strictly(WorkflowGraph)
    ),
    WorkflowFormatVersion.V2: WorkflowDocumentFormat(
        WorkflowGraphV2, _strictly(WorkflowGraphV2)
    ),
    WorkflowFormatVersion.V3: WorkflowDocumentFormat(
        WorkflowGraphV3, validate_workflow_graph_v3
    ),
}
"""Every format version a published document may declare, and nothing else.

V3 does not read through `_strictly`: it refuses a retired key by the name that
replaced it before the model ever sees the document, and translates what the
model refuses into the named refusal an author can act on.
"""
