from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from atelier2.contracts.runs import WorkflowRevisionHash
from atelier2.contracts.workflows_v3 import VersionedReference, WorkflowGraphV3


class SubworkflowBindingRefusalReason(StrEnum):
    """Why one subworkflow node cannot bind its child revision, as a stable token."""

    MALFORMED_WORKFLOW_REFERENCE = "malformed_workflow_reference"
    UNPUBLISHED_WORKFLOW_REFERENCE = "unpublished_workflow_reference"
    RESOLVED_REVISION_MISMATCH = "resolved_revision_mismatch"
    CHILD_DOCUMENT_REFUSED = "child_document_refused"
    CHILD_FORMAT_UNSUPPORTED = "child_format_unsupported"
    MISSING_INPUT_BINDING = "missing_input_binding"
    EXTRA_INPUT_BINDING = "extra_input_binding"
    MISSING_OUTPUT_BINDING = "missing_output_binding"
    EXTRA_OUTPUT_BINDING = "extra_output_binding"
    UNPROVEN_INPUT_SCHEMA = "unproven_input_schema"
    SCHEMA_REVISION_MISMATCH = "schema_revision_mismatch"
    NESTING_DEPTH_EXCEEDED = "nesting_depth_exceeded"
    ITERATION_ROUNDS_EXCEEDED = "iteration_rounds_exceeded"


@dataclass(frozen=True, slots=True)
class SubworkflowBindingRefusal:
    """The named subject of one binding refusal: which node, through which chain."""

    reason: SubworkflowBindingRefusalReason
    node: str
    chain: tuple[VersionedReference, ...]
    detail: str

    def __post_init__(self) -> None:
        if self.node == "":
            raise ValueError("a binding refusal names the node it refuses")
        if not self.chain:
            raise ValueError("a binding refusal names the chain it was reached through")

    @property
    def reference(self) -> VersionedReference:
        """The child revision the refused node names."""
        return self.chain[-1]

    def __str__(self) -> str:
        chain = " > ".join(
            f"{reference.ref}@{reference.revision}" for reference in self.chain
        )
        return (
            f"node {self.node!r} binding {chain}: {self.detail} [{self.reason.value}]"
        )


class SubworkflowBindingRefused(Exception):
    """One subworkflow node names a child its declared boundary cannot bind."""

    def __init__(self, refusal: SubworkflowBindingRefusal) -> None:
        super().__init__(str(refusal))
        self.refusal = refusal


@dataclass(frozen=True)
class BoundSubworkflow:
    """One subworkflow node bound to the exact published child revision it reuses."""

    node_id: str
    reference: VersionedReference
    child_revision_hash: WorkflowRevisionHash
    child: WorkflowGraphV3
    children: tuple[BoundSubworkflow, ...] = ()

    @property
    def nesting_depth(self) -> int:
        """How deep the chain rooted at this node runs, counting this child as one."""
        return 1 + max((child.nesting_depth for child in self.children), default=0)


@dataclass(frozen=True)
class SubworkflowBinding:
    """Every subworkflow node of one document, bound to real child content."""

    subworkflows: tuple[BoundSubworkflow, ...] = ()

    @property
    def nesting_depth(self) -> int:
        return max((bound.nesting_depth for bound in self.subworkflows), default=0)


class ReceiptDisposition(StrEnum):
    """How one child node's terminal receipt ended."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ChildSinkReceipt:
    node_id: str
    disposition: ReceiptDisposition


@dataclass(frozen=True)
class ChildRunReport:
    """What a child run leaves for its parent node to read.

    A sink absent from `sink_receipts` never reached a terminal receipt, which is
    how an unfinished child run is expressed without a second copy of run state.
    """

    sink_receipts: tuple[ChildSinkReceipt, ...]
    produced_graph_outputs: tuple[str, ...]
    cancelled: bool


@dataclass(frozen=True)
class SubworkflowSucceeded:
    delivered_graph_outputs: tuple[str, ...]


@dataclass(frozen=True)
class SubworkflowCancelled:
    @property
    def delivered_graph_outputs(self) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True)
class SubworkflowFailed:
    """The named child sink that broke the parent, and the value it never produced."""

    sink_node_id: str
    missing_graph_output: str | None

    @property
    def delivered_graph_outputs(self) -> tuple[str, ...]:
        return ()


type ParentDisposition = SubworkflowSucceeded | SubworkflowCancelled | SubworkflowFailed


def decide_parent_disposition(
    bound: BoundSubworkflow, report: ChildRunReport
) -> ParentDisposition:
    """The parent node's disposition for one finished child run.

    A sink sourcing no graph output still performed the child's work, so its
    failure is the child's failure even when every output-sourcing sink succeeded.
    Only a succeeded disposition delivers values; the other two deliver none.
    """
    sinks = bound.child.sink_node_ids
    receipts = {
        receipt.node_id: receipt.disposition for receipt in report.sink_receipts
    }
    foreign = sorted(set(receipts) - set(sinks))
    if foreign:
        raise ValueError(
            f"child run report carries receipts for non-sink nodes: {foreign}"
        )
    if report.cancelled:
        return SubworkflowCancelled()
    for sink in sinks:
        if receipts.get(sink) is not ReceiptDisposition.SUCCEEDED:
            return SubworkflowFailed(sink, None)
    produced = set(report.produced_graph_outputs)
    for graph_output in bound.child.graph_outputs:
        if graph_output.name not in produced:
            return SubworkflowFailed(graph_output.source.node, graph_output.name)
    return SubworkflowSucceeded(
        tuple(graph_output.name for graph_output in bound.child.graph_outputs)
    )
