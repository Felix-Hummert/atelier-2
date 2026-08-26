from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class WorkflowRefusalReason(StrEnum):
    """Why one closed workflow document was refused, as a stable token."""

    UNKNOWN_FIELD = "unknown_field"
    REFUSED_FIELD = "refused_field"
    RETIRED_KEY = "retired_key"
    MISSING_FIELD = "missing_field"
    INVALID_VALUE = "invalid_value"
    MALFORMED_DOCUMENT = "malformed_document"
    FORBIDDEN_YAML_FEATURE = "forbidden_yaml_feature"
    DUPLICATE_KEY = "duplicate_key"
    MULTIPLE_DOCUMENTS = "multiple_documents"
    DOCUMENT_TOO_DEEP = "document_too_deep"
    DUPLICATE_NODE_ID = "duplicate_node_id"
    DUPLICATE_NAME = "duplicate_name"
    UNKNOWN_NODE_REFERENCE = "unknown_node_reference"
    CYCLE = "cycle"
    JOIN_WITHOUT_DEPENDENCY = "join_without_dependency"
    JOIN_REQUIRED = "join_required"
    DATA_EDGE_OUTSIDE_CLOSURE = "data_edge_outside_closure"
    UNDECLARED_OUTPUT = "undeclared_output"
    UNDECLARED_CONTEXT = "undeclared_context"
    UNDECLARED_GRAPH_INPUT = "undeclared_graph_input"
    UNDECLARED_ROLE = "undeclared_role"
    GRAPH_OUTPUT_NOT_SINK = "graph_output_not_sink"
    GRAPH_INPUT_UNREAD = "graph_input_unread"
    UNCONFIRMED_INTERACTIVE_OUTPUT = "unconfirmed_interactive_output"
    CONFIRMATION_WITHOUT_INTERACTIVE_MODE = "confirmation_without_interactive_mode"
    UNBOUNDED_ITERATION = "unbounded_iteration"
    ITERATION_GREEN_CONDITION_UNPROVABLE = "iteration_green_condition_unprovable"
    ITERATION_CARRY_UNBOUND = "iteration_carry_unbound"
    LOOP_BODY_NOT_ONE_LINE = "loop_body_not_one_line"
    LOOP_VERDICT_NODE_NOT_THE_ROUND_END = "loop_verdict_node_not_the_round_end"
    LOOP_VERDICT_UNREADABLE = "loop_verdict_unreadable"


@dataclass(frozen=True, slots=True)
class WorkflowRefusal:
    """The named subject of one refusal: which node, which field, and why."""

    reason: WorkflowRefusalReason
    field: str
    detail: str
    node: str | None = None

    def __str__(self) -> str:
        subject = (
            "the workflow document" if self.node is None else f"node {self.node!r}"
        )
        return f"{subject} field {self.field!r}: {self.detail} [{self.reason.value}]"


class WorkflowDocumentRefused(Exception):
    """A closed workflow document names a form its format refuses."""

    def __init__(self, refusal: WorkflowRefusal) -> None:
        super().__init__(str(refusal))
        self.refusal = refusal


class WorkflowDocumentInvalid(ValueError):
    """Exact bytes are not one valid workflow document.

    Carries the named refusal whenever the format could name one, so a caller
    above the parsing adapter can report the node and field without reading
    prose.
    """

    def __init__(self, detail: str, refusal: WorkflowRefusal | None = None) -> None:
        super().__init__(detail)
        self.refusal = refusal
