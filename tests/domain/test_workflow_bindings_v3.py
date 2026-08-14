from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import pytest

from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.application.bind_subworkflow_boundaries import (
    bind_subworkflow_boundaries,
)
from atelier2.contracts.runs import WorkflowRevision
from atelier2.contracts.workflow_bindings_v3 import (
    BoundSubworkflow,
    ChildRunReport,
    ChildSinkReceipt,
    ParentDisposition,
    ReceiptDisposition,
    SubworkflowBinding,
    SubworkflowBindingRefusalReason,
    SubworkflowBindingRefused,
    SubworkflowCancelled,
    SubworkflowFailed,
    SubworkflowSucceeded,
    decide_parent_disposition,
)
from atelier2.contracts.workflow_refusals import WorkflowRefusalReason
from atelier2.contracts.workflows_v3 import VersionedReference, WorkflowGraphV3
from atelier2.ports.workflow_revisions import (
    PublishedWorkflowFound,
    PublishedWorkflowMissing,
    ResolvePublishedWorkflowResult,
)

PARENT = b"""format_version: 3
name: Build a candidate, then hand it to the review panel
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Build every acceptance sentence of the bound story.
    outputs:
      - name: candidate
        schema: {ref: workspace_candidate, revision: schema-candidate}
  - id: review_panel
    type: subworkflow
    depends_on: [implement]
    workflow: {ref: review_panel, revision: workflow-1}
    inputs:
      - name: candidate
        from: {node: implement, output: candidate}
    outputs:
      - name: verdict
        schema: {ref: review_verdict, revision: schema-verdict}
"""

CHILD = b"""format_version: 3
name: Review a candidate and merge the verdicts
graph_inputs:
  - name: candidate
    schema: {ref: workspace_candidate, revision: schema-candidate}
graph_outputs:
  - name: verdict
    from: {node: merge_findings, output: merged}
nodes:
  - id: merge_findings
    type: deterministic
    operation: {ref: merge_review_verdicts, revision: operation-1}
    outputs:
      - name: merged
        schema: {ref: review_verdict, revision: schema-verdict}
  - id: hand_off
    type: action
    operation: {ref: requirement_comment, revision: operation-2}
    outputs:
      - name: receipt
        schema: {ref: comment_receipt, revision: schema-receipt}
"""

NESTING_CHILD = b"""format_version: 3
name: Seed an inner panel and pass its verdict up
graph_inputs:
  - name: candidate
    schema: {ref: workspace_candidate, revision: schema-candidate}
graph_outputs:
  - name: verdict
    from: {node: inner_panel, output: verdict}
nodes:
  - id: inner_panel
    type: subworkflow
    workflow: {ref: inner_panel, revision: workflow-2}
    inputs:
      - name: seed
        value: rehearse the panel
    outputs:
      - name: verdict
        schema: {ref: review_verdict, revision: schema-verdict}
"""

LEAF_CHILD = b"""format_version: 3
name: Decide a verdict alone, without a panel
graph_inputs:
  - name: seed
    schema: {ref: seed_note, revision: schema-seed}
graph_outputs:
  - name: verdict
    from: {node: decide, output: verdict}
nodes:
  - id: decide
    type: deterministic
    operation: {ref: decide_alone, revision: operation-3}
    outputs:
      - name: verdict
        schema: {ref: review_verdict, revision: schema-verdict}
"""

SELF_NAMING_CHILD = b"""format_version: 3
name: Name the very panel this document is
graph_inputs:
  - name: candidate
    schema: {ref: workspace_candidate, revision: schema-candidate}
graph_outputs:
  - name: verdict
    from: {node: again, output: verdict}
nodes:
  - id: again
    type: subworkflow
    workflow: {ref: review_panel, revision: workflow-1}
    inputs:
      - name: candidate
        value: the same panel once more
    outputs:
      - name: verdict
        schema: {ref: review_verdict, revision: schema-verdict}
"""

V2_CHILD = b"""format_version: 2
start: build
nodes:
  - {id: done, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: build, type: agent, role: builder, job: implement, next: done}
"""

BOUND_INPUT = b"""    inputs:
      - name: candidate
        from: {node: implement, output: candidate}
"""

BOUND_OUTPUT = b"""    outputs:
      - name: verdict
        schema: {ref: review_verdict, revision: schema-verdict}
"""

UNASKED_FOR_INPUT = b"""      - name: surprise
        value: unasked for
"""

UNASKED_FOR_OUTPUT = b"""      - name: surprise
        schema: {ref: surprise_note, revision: schema-surprise}
"""

REVIEW_PANEL = ("review_panel", "workflow-1")
INNER_PANEL = ("inner_panel", "workflow-2")

PUBLISHED: Mapping[tuple[str, str], bytes] = {REVIEW_PANEL: CHILD}
PUBLISHED_NESTING: Mapping[tuple[str, str], bytes] = {
    REVIEW_PANEL: NESTING_CHILD,
    INNER_PANEL: LEAF_CHILD,
}


@dataclass(frozen=True)
class PublishedWorkflows:
    """The publication store as this binding reads it: references to exact bytes."""

    documents: Mapping[tuple[str, str], bytes]

    def resolve(self, reference: VersionedReference) -> ResolvePublishedWorkflowResult:
        document = self.documents.get((reference.ref, reference.revision))
        if document is None:
            return PublishedWorkflowMissing()
        return PublishedWorkflowFound(WorkflowRevision(document))


def parsed(document: bytes) -> WorkflowGraphV3:
    graph = parse_workflow_document(document)
    assert isinstance(graph, WorkflowGraphV3)
    return graph


def bind(
    document: bytes = PARENT,
    published: Mapping[tuple[str, str], bytes] = PUBLISHED,
    maximum_nesting_depth: int = 2,
) -> SubworkflowBinding:
    return bind_subworkflow_boundaries(
        parsed(document),
        PublishedWorkflows(published),
        parse_workflow_document,
        maximum_nesting_depth,
    )


def without_lines(first: str, count: int, document: bytes = PARENT) -> bytes:
    lines = document.decode("utf-8").splitlines(keepends=True)
    start = next(index for index, line in enumerate(lines) if line.startswith(first))
    del lines[start : start + count]
    return "".join(lines).encode("utf-8")


def test_a_subworkflow_node_binds_the_published_child_revision_it_names() -> None:
    binding = bind()

    bound = binding.subworkflows[0]
    assert (bound.node_id, bound.reference.ref) == ("review_panel", "review_panel")
    assert bound.child_revision_hash == WorkflowRevision(CHILD).revision_hash
    assert [entry.name for entry in bound.child.graph_inputs] == ["candidate"]
    assert [entry.name for entry in bound.child.graph_outputs] == ["verdict"]
    assert binding.nesting_depth == 1


def test_a_document_naming_no_child_binds_to_an_empty_boundary() -> None:
    binding = bind(document=without_lines("  - id: review_panel", 10))

    assert binding.subworkflows == ()
    assert binding.nesting_depth == 0


def test_a_nested_child_binds_through_the_chain_it_was_reached_by() -> None:
    binding = bind(published=PUBLISHED_NESTING)

    bound = binding.subworkflows[0]
    nested = bound.children[0]
    assert (nested.node_id, nested.reference.ref) == ("inner_panel", "inner_panel")
    assert nested.child_revision_hash == WorkflowRevision(LEAF_CHILD).revision_hash
    assert (bound.nesting_depth, binding.nesting_depth) == (2, 2)


def test_a_child_graph_output_reading_no_sink_is_refused_at_binding() -> None:
    depending_on_the_source = CHILD.replace(
        b"  - id: hand_off\n    type: action\n",
        b"  - id: hand_off\n    type: action\n    depends_on: [merge_findings]\n",
    )

    with pytest.raises(SubworkflowBindingRefused) as raised:
        bind(published={REVIEW_PANEL: depending_on_the_source})

    refusal = raised.value.refusal
    assert refusal.reason is SubworkflowBindingRefusalReason.CHILD_DOCUMENT_REFUSED
    assert refusal.node == "review_panel"
    assert WorkflowRefusalReason.GRAPH_OUTPUT_NOT_SINK.value in refusal.detail
    assert "'verdict'" in refusal.detail and "'merge_findings'" in refusal.detail


@dataclass(frozen=True)
class Refusal:
    reason: SubworkflowBindingRefusalReason
    node: str
    chain: tuple[tuple[str, str], ...]
    document: bytes = PARENT
    published: Mapping[tuple[str, str], bytes] = field(
        default_factory=lambda: PUBLISHED
    )
    maximum_nesting_depth: int = 2


REFUSALS: Mapping[str, Refusal] = {
    "reference-no-publication-carries": Refusal(
        SubworkflowBindingRefusalReason.UNPUBLISHED_WORKFLOW_REFERENCE,
        "review_panel",
        (REVIEW_PANEL,),
        published={},
    ),
    "child-of-an-older-format": Refusal(
        SubworkflowBindingRefusalReason.CHILD_FORMAT_UNSUPPORTED,
        "review_panel",
        (REVIEW_PANEL,),
        published={REVIEW_PANEL: V2_CHILD},
    ),
    "graph-input-the-node-never-binds": Refusal(
        SubworkflowBindingRefusalReason.MISSING_INPUT_BINDING,
        "review_panel",
        (REVIEW_PANEL,),
        document=PARENT.replace(BOUND_INPUT, b""),
    ),
    "input-the-child-never-declares": Refusal(
        SubworkflowBindingRefusalReason.EXTRA_INPUT_BINDING,
        "review_panel",
        (REVIEW_PANEL,),
        document=PARENT.replace(BOUND_INPUT, BOUND_INPUT + UNASKED_FOR_INPUT),
    ),
    "graph-output-the-node-never-declares": Refusal(
        SubworkflowBindingRefusalReason.MISSING_OUTPUT_BINDING,
        "review_panel",
        (REVIEW_PANEL,),
        document=PARENT.replace(BOUND_OUTPUT, b""),
    ),
    "output-the-child-never-produces": Refusal(
        SubworkflowBindingRefusalReason.EXTRA_OUTPUT_BINDING,
        "review_panel",
        (REVIEW_PANEL,),
        document=PARENT.replace(BOUND_OUTPUT, BOUND_OUTPUT + UNASKED_FOR_OUTPUT),
    ),
    "input-schema-of-another-revision": Refusal(
        SubworkflowBindingRefusalReason.SCHEMA_REVISION_MISMATCH,
        "review_panel",
        (REVIEW_PANEL,),
        document=PARENT.replace(
            b"schema: {ref: workspace_candidate, revision: schema-candidate}",
            b"schema: {ref: workspace_candidate, revision: schema-candidate-2}",
        ),
    ),
    "output-schema-of-another-revision": Refusal(
        SubworkflowBindingRefusalReason.SCHEMA_REVISION_MISMATCH,
        "review_panel",
        (REVIEW_PANEL,),
        document=PARENT.replace(
            b"schema: {ref: review_verdict, revision: schema-verdict}",
            b"schema: {ref: review_verdict, revision: schema-verdict-2}",
        ),
    ),
    "chain-deeper-than-the-attested-maximum": Refusal(
        SubworkflowBindingRefusalReason.NESTING_DEPTH_EXCEEDED,
        "inner_panel",
        (REVIEW_PANEL, INNER_PANEL),
        published=PUBLISHED_NESTING,
        maximum_nesting_depth=1,
    ),
    "revision-reached-twice-in-one-chain": Refusal(
        SubworkflowBindingRefusalReason.RECURSIVE_WORKFLOW_REFERENCE,
        "again",
        (REVIEW_PANEL, REVIEW_PANEL),
        published={REVIEW_PANEL: SELF_NAMING_CHILD},
    ),
}


@pytest.mark.parametrize("case", REFUSALS.values(), ids=REFUSALS)
def test_every_refused_binding_names_its_node_and_the_chain(case: Refusal) -> None:
    with pytest.raises(SubworkflowBindingRefused) as raised:
        bind(case.document, case.published, case.maximum_nesting_depth)

    refusal = raised.value.refusal
    chain = tuple((entry.ref, entry.revision) for entry in refusal.chain)
    assert (refusal.reason, refusal.node, chain) == (case.reason, case.node, case.chain)


def test_the_attested_depth_admits_exactly_the_chain_it_proves() -> None:
    assert bind(published=PUBLISHED_NESTING, maximum_nesting_depth=2).nesting_depth == 2


def test_a_negative_depth_bound_is_refused_before_any_reference_resolves() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        bind(published={}, maximum_nesting_depth=-1)


def bound_child() -> BoundSubworkflow:
    return bind().subworkflows[0]


def receipts(**dispositions: ReceiptDisposition) -> tuple[ChildSinkReceipt, ...]:
    return tuple(
        ChildSinkReceipt(node_id, disposition)
        for node_id, disposition in dispositions.items()
    )


BOTH_SINKS_SUCCEEDED = receipts(
    merge_findings=ReceiptDisposition.SUCCEEDED,
    hand_off=ReceiptDisposition.SUCCEEDED,
)

DISPOSITIONS: Mapping[str, tuple[ChildRunReport, ParentDisposition]] = {
    "every-sink-succeeded-and-every-output-arrived": (
        ChildRunReport(BOTH_SINKS_SUCCEEDED, ("verdict",), cancelled=False),
        SubworkflowSucceeded(("verdict",)),
    ),
    "the-child-run-was-cancelled": (
        ChildRunReport(BOTH_SINKS_SUCCEEDED, ("verdict",), cancelled=True),
        SubworkflowCancelled(),
    ),
    "cancellation-outranks-a-broken-sink": (
        ChildRunReport(
            receipts(
                merge_findings=ReceiptDisposition.FAILED,
                hand_off=ReceiptDisposition.CANCELLED,
            ),
            (),
            cancelled=True,
        ),
        SubworkflowCancelled(),
    ),
    "the-output-sourcing-sink-failed": (
        ChildRunReport(
            receipts(
                merge_findings=ReceiptDisposition.FAILED,
                hand_off=ReceiptDisposition.SUCCEEDED,
            ),
            (),
            cancelled=False,
        ),
        SubworkflowFailed("merge_findings", None),
    ),
    "the-handoff-sink-sourcing-no-output-failed": (
        ChildRunReport(
            receipts(
                merge_findings=ReceiptDisposition.SUCCEEDED,
                hand_off=ReceiptDisposition.FAILED,
            ),
            ("verdict",),
            cancelled=False,
        ),
        SubworkflowFailed("hand_off", None),
    ),
    "a-sink-never-reached-a-terminal-receipt": (
        ChildRunReport(
            receipts(merge_findings=ReceiptDisposition.SUCCEEDED),
            ("verdict",),
            cancelled=False,
        ),
        SubworkflowFailed("hand_off", None),
    ),
    "every-sink-succeeded-but-the-output-never-arrived": (
        ChildRunReport(BOTH_SINKS_SUCCEEDED, (), cancelled=False),
        SubworkflowFailed("merge_findings", "verdict"),
    ),
}


@pytest.mark.parametrize(
    ("report", "expected"), DISPOSITIONS.values(), ids=DISPOSITIONS
)
def test_the_parent_disposition_reads_the_whole_child_and_only_delivers_on_success(
    report: ChildRunReport, expected: ParentDisposition
) -> None:
    disposition = decide_parent_disposition(bound_child(), report)

    assert disposition == expected
    assert disposition.delivered_graph_outputs == expected.delivered_graph_outputs


def test_a_child_run_report_naming_a_node_that_is_no_sink_is_refused_loudly() -> None:
    report = ChildRunReport(
        receipts(implement=ReceiptDisposition.SUCCEEDED), ("verdict",), cancelled=False
    )

    with pytest.raises(ValueError, match="non-sink"):
        decide_parent_disposition(bound_child(), report)
