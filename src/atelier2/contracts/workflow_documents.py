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


WORKFLOW_DOCUMENT_FORMATS: Mapping[WorkflowFormatVersion, WorkflowDocumentFormat] = {
    WorkflowFormatVersion.V3: WorkflowDocumentFormat(
        WorkflowGraphV3, validate_workflow_graph_v3
    ),
}
"""Every format version a published document may declare, and nothing else.

V1 and V2 stay named `WorkflowFormatVersion` members -- the durable layer's
`runs.workflow_format_version` column and its frozen schema still read those
historical values -- but no model reads a document that declares one anymore
(#901 slice 5): the retired key is refused by name in `parse_workflow_document`
rather than reaching this table as an unhandled key.
"""
