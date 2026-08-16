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

PINNED_REVISION = b"<revision>"

PARENT_TEMPLATE = b"""format_version: 3
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
    workflow: {ref: review_panel, revision: <revision>}
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
    inputs:
      - name: candidate
        from: {graph_input: candidate}
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
    inputs:
      - name: seed
        from: {graph_input: seed}
    outputs:
      - name: verdict
        schema: {ref: review_verdict, revision: schema-verdict}
"""

NESTING_CHILD_TEMPLATE = b"""format_version: 3
name: Seed an inner panel and pass its verdict up
graph_inputs:
  - name: candidate
    schema: {ref: workspace_candidate, revision: schema-candidate}
graph_outputs:
  - name: verdict
    from: {node: inner_panel, output: verdict}
nodes:
  - id: seed_the_panel
    type: deterministic
    operation: {ref: seed_the_panel, revision: operation-4}
    inputs:
      - name: candidate
        from: {graph_input: candidate}
    outputs:
      - name: seed
        schema: {ref: seed_note, revision: schema-seed}
  - id: inner_panel
    type: subworkflow
    depends_on: [seed_the_panel]
    workflow: {ref: inner_panel, revision: <revision>}
    inputs:
      - name: seed
        from: {node: seed_the_panel, output: seed}
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


def revision_of(document: bytes) -> str:
    """The exact revision hash a reference must name to bind these bytes."""
    return WorkflowRevision(document).revision_hash.value


def pinning(template: bytes, child: bytes) -> bytes:
    """One document naming the child revision it reuses, by that child's hash."""
    return template.replace(PINNED_REVISION, revision_of(child).encode("utf-8"))


def carrying(**children: bytes) -> dict[tuple[str, str], bytes]:
    return {(ref, revision_of(child)): child for ref, child in children.items()}


ORDER_DECLARATION = b"""graph_inputs:
  - name: order
    schema: {ref: workspace_candidate, revision: schema-candidate}
"""

ORDERED_PARENT_TEMPLATE = PARENT_TEMPLATE.replace(
    b"nodes:\n", ORDER_DECLARATION + b"nodes:\n"
).replace(
    BOUND_INPUT,
    b"""    inputs:
      - name: candidate
        from: {graph_input: order}
""",
)

PARENT = pinning(PARENT_TEMPLATE, CHILD)
ORDERED_PARENT = pinning(ORDERED_PARENT_TEMPLATE, CHILD)
NESTING_CHILD = pinning(NESTING_CHILD_TEMPLATE, LEAF_CHILD)
NESTING_PARENT = pinning(PARENT_TEMPLATE, NESTING_CHILD)

REVIEW_PANEL = ("review_panel", revision_of(CHILD))
NESTED_REVIEW_PANEL = ("review_panel", revision_of(NESTING_CHILD))
INNER_PANEL = ("inner_panel", revision_of(LEAF_CHILD))

PUBLISHED = carrying(review_panel=CHILD)
PUBLISHED_NESTING = carrying(review_panel=NESTING_CHILD, inner_panel=LEAF_CHILD)


@dataclass
class PublishedWorkflows:
    """The publication store as this binding reads it: references to exact bytes.

    It records what it was asked, so a test can prove a refusal happened before
    any reference was resolved.
    """

    documents: Mapping[tuple[str, str], bytes]
    answers: bytes | None = None
    resolutions: list[VersionedReference] = field(default_factory=list)

    def resolve(self, reference: VersionedReference) -> ResolvePublishedWorkflowResult:
        self.resolutions.append(reference)
        if self.answers is not None:
            return PublishedWorkflowFound(WorkflowRevision(self.answers))
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
    store: PublishedWorkflows | None = None,
    maximum_iteration_rounds: int = 8,
) -> SubworkflowBinding:
    return bind_subworkflow_boundaries(
        parsed(document),
        store if store is not None else PublishedWorkflows(published),
        parse_workflow_document,
        maximum_nesting_depth,
        maximum_iteration_rounds,
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


@pytest.mark.proves("a-parent-graph-input-reaches-a-child-through-the-boundary")
def test_an_order_the_parent_was_started_with_binds_to_the_graph_input_of_its_child() -> (
    None
):
    binding = bind(document=ORDERED_PARENT)

    bound = binding.subworkflows[0]
    assert (bound.node_id, bound.reference.ref) == ("review_panel", "review_panel")
    assert [entry.name for entry in bound.child.graph_inputs] == ["candidate"]
    assert bound.child_revision_hash == WorkflowRevision(CHILD).revision_hash


@pytest.mark.proves("a-parent-graph-input-reaches-a-child-through-the-boundary")
def test_an_order_declared_under_another_schema_revision_than_the_child_is_refused() -> (
    None
):
    disagreeing = ORDERED_PARENT.replace(
        b"    schema: {ref: workspace_candidate, revision: schema-candidate}\n",
        b"    schema: {ref: workspace_candidate, revision: schema-other}\n",
    )
    assert disagreeing != ORDERED_PARENT

    with pytest.raises(SubworkflowBindingRefused) as raised:
        bind(document=disagreeing)

    refusal = raised.value.refusal
    assert refusal.reason is SubworkflowBindingRefusalReason.SCHEMA_REVISION_MISMATCH
    assert refusal.node == "review_panel"
    assert "schema-other" in str(refusal)


def test_a_document_naming_no_child_binds_to_an_empty_boundary() -> None:
    binding = bind(document=without_lines("  - id: review_panel", 10))

    assert binding.subworkflows == ()
    assert binding.nesting_depth == 0


def test_a_nested_child_binds_through_the_chain_it_was_reached_by() -> None:
    binding = bind(document=NESTING_PARENT, published=PUBLISHED_NESTING)

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
        bind(
            document=pinning(PARENT_TEMPLATE, depending_on_the_source),
            published=carrying(review_panel=depending_on_the_source),
        )

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
        (("review_panel", revision_of(V2_CHILD)),),
        document=pinning(PARENT_TEMPLATE, V2_CHILD),
        published=carrying(review_panel=V2_CHILD),
    ),
    "reference-naming-no-revision-hash": Refusal(
        SubworkflowBindingRefusalReason.MALFORMED_WORKFLOW_REFERENCE,
        "review_panel",
        (("review_panel", "workflow-1"),),
        document=PARENT_TEMPLATE.replace(PINNED_REVISION, b"workflow-1"),
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
        (NESTED_REVIEW_PANEL, INNER_PANEL),
        document=NESTING_PARENT,
        published=PUBLISHED_NESTING,
        maximum_nesting_depth=1,
    ),
    "input-proving-no-schema-revision": Refusal(
        SubworkflowBindingRefusalReason.UNPROVEN_INPUT_SCHEMA,
        "review_panel",
        (REVIEW_PANEL,),
        document=PARENT.replace(
            BOUND_INPUT,
            b"    inputs:\n      - name: candidate\n        value: the panel reads this\n",
        ),
    ),
    "input-sourced-from-a-terminal-receipt": Refusal(
        SubworkflowBindingRefusalReason.UNPROVEN_INPUT_SCHEMA,
        "review_panel",
        (REVIEW_PANEL,),
        document=PARENT.replace(
            b"        from: {node: implement, output: candidate}\n",
            b"        from: {node: implement, receipt: terminal}\n",
        ),
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
    binding = bind(
        document=NESTING_PARENT, published=PUBLISHED_NESTING, maximum_nesting_depth=2
    )

    assert binding.nesting_depth == 2


def test_a_store_answering_with_another_revision_is_refused_before_it_binds() -> None:
    store = PublishedWorkflows(PUBLISHED, answers=LEAF_CHILD)

    with pytest.raises(SubworkflowBindingRefused) as raised:
        bind(store=store)

    refusal = raised.value.refusal
    assert refusal.reason is SubworkflowBindingRefusalReason.RESOLVED_REVISION_MISMATCH
    assert refusal.node == "review_panel"
    assert revision_of(CHILD) in refusal.detail
    assert revision_of(LEAF_CHILD) in refusal.detail


def test_a_bound_child_carries_the_exact_revision_its_reference_named() -> None:
    bound = bind().subworkflows[0]

    assert bound.reference.revision == revision_of(CHILD)
    assert bound.child_revision_hash.value == bound.reference.revision


def test_the_depth_bound_is_spent_before_any_child_is_resolved_or_read() -> None:
    store = PublishedWorkflows(PUBLISHED)

    with pytest.raises(SubworkflowBindingRefused) as raised:
        bind(maximum_nesting_depth=0, store=store)

    assert (
        raised.value.refusal.reason
        is SubworkflowBindingRefusalReason.NESTING_DEPTH_EXCEEDED
    )
    assert store.resolutions == []


def test_a_malformed_reference_is_refused_before_any_child_is_resolved() -> None:
    store = PublishedWorkflows(PUBLISHED)

    with pytest.raises(SubworkflowBindingRefused):
        bind(
            document=PARENT_TEMPLATE.replace(PINNED_REVISION, b"workflow-1"),
            store=store,
        )

    assert store.resolutions == []


def test_one_schema_revision_binds_under_whichever_entry_name_names_it() -> None:
    under_another_entry_name = PARENT.replace(
        b"schema: {ref: workspace_candidate, revision: schema-candidate}",
        b"schema: {ref: reviewed_candidate, revision: schema-candidate}",
    )

    binding = bind(document=under_another_entry_name)

    assert binding.subworkflows[0].child_revision_hash.value == revision_of(CHILD)


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


ITERATING_PARENT_TEMPLATE = PARENT_TEMPLATE.replace(
    b"""    outputs:
      - name: candidate
        schema: {ref: workspace_candidate, revision: schema-candidate}
""",
    b"""    outputs:
      - name: candidate
        schema: {ref: workspace_candidate, revision: schema-candidate}
      - name: first_verdict
        schema: {ref: review_verdict, revision: schema-verdict}
""",
).replace(
    b"""      - name: verdict
        schema: {ref: review_verdict, revision: schema-verdict}
""",
    b"""      - name: verdict
        schema: {ref: review_verdict, revision: schema-verdict}
    iterate:
      maximum_rounds: 4
      until:
        output: verdict
        schema: {ref: review_verdict, revision: schema-verdict}
      carry:
        - name: previous_verdict
          from_output: verdict
          seed: {node: implement, output: first_verdict}
""",
)

# The child of an iterating node declares one order it is handed every round and
# one it is handed only from the round before, so the carry is what satisfies the
# second -- a node input never does.
ITERATING_CHILD = CHILD.replace(
    b"""graph_inputs:
  - name: candidate
    schema: {ref: workspace_candidate, revision: schema-candidate}""",
    b"""graph_inputs:
  - name: candidate
    schema: {ref: workspace_candidate, revision: schema-candidate}
  - name: previous_verdict
    schema: {ref: review_verdict, revision: schema-verdict}""",
).replace(
    b"""      - name: candidate
        from: {graph_input: candidate}""",
    b"""      - name: candidate
        from: {graph_input: candidate}
      - name: previous_verdict
        from: {graph_input: previous_verdict}""",
)
ITERATING_PARENT = pinning(ITERATING_PARENT_TEMPLATE, ITERATING_CHILD)
ITERATING_PUBLISHED = carrying(review_panel=ITERATING_CHILD)


def bind_iterating(
    document: bytes = ITERATING_PARENT,
    published: Mapping[tuple[str, str], bytes] | None = None,
    maximum_iteration_rounds: int = 8,
) -> SubworkflowBinding:
    return bind(
        document=document,
        published=ITERATING_PUBLISHED if published is None else published,
        maximum_iteration_rounds=maximum_iteration_rounds,
    )


@pytest.mark.proves("a-round-handover-the-child-cannot-bind-is-refused-by-name")
def test_a_carried_order_the_node_never_supplies_still_binds_to_the_child() -> None:
    """The carry is what fills it, so the boundary is whole without a node input.

    Every other child order is matched one to one against the node's inputs. A
    carried one is supplied by the round before it — and by its seed in the first
    round — so demanding a node input for it would refuse the very shape the
    iteration exists to express.
    """
    binding = bind_iterating()

    bound = binding.subworkflows[0]
    assert [entry.name for entry in bound.child.graph_inputs] == [
        "candidate",
        "previous_verdict",
    ]


@pytest.mark.proves("a-green-condition-the-child-cannot-answer-is-refused-by-name")
def test_a_green_condition_naming_no_result_of_the_child_is_refused() -> None:
    unanswerable = ITERATING_PARENT.replace(
        b"      until:\n        output: verdict\n",
        b"      until:\n        output: rumour\n",
    )
    assert unanswerable != ITERATING_PARENT

    with pytest.raises(SubworkflowBindingRefused) as raised:
        bind_iterating(document=unanswerable)

    refusal = raised.value.refusal
    assert refusal.node == "review_panel"
    assert "rumour" in str(refusal)


@pytest.mark.proves("a-round-handover-the-child-cannot-bind-is-refused-by-name")
def test_a_handover_reading_a_result_the_child_never_declares_is_refused() -> None:
    unreadable = ITERATING_PARENT.replace(
        b"          from_output: verdict\n", b"          from_output: rumour\n"
    )
    assert unreadable != ITERATING_PARENT

    with pytest.raises(SubworkflowBindingRefused) as raised:
        bind_iterating(document=unreadable)

    assert "rumour" in str(raised.value.refusal)


@pytest.mark.proves("a-round-handover-the-child-cannot-bind-is-refused-by-name")
def test_a_handover_the_child_accepts_under_another_schema_is_refused() -> None:
    """Only the carried order's schema differs, so nothing else can raise this."""
    disagreeing = ITERATING_CHILD.replace(
        b"""  - name: previous_verdict
    schema: {ref: review_verdict, revision: schema-verdict}""",
        b"""  - name: previous_verdict
    schema: {ref: review_verdict, revision: schema-other}""",
    )
    assert disagreeing != ITERATING_CHILD
    document = pinning(ITERATING_PARENT_TEMPLATE, disagreeing)

    with pytest.raises(SubworkflowBindingRefused) as raised:
        bind_iterating(document=document, published=carrying(review_panel=disagreeing))

    assert (
        raised.value.refusal.reason
        is SubworkflowBindingRefusalReason.SCHEMA_REVISION_MISMATCH
    )


@pytest.mark.parametrize(
    "seeded_schema",
    (
        b"{ref: review_verdict, revision: schema-other}",
        b"{ref: landing_approval, revision: schema-approval}",
    ),
    ids=("another revision of the same schema", "another schema entirely"),
)
@pytest.mark.proves("a-round-handover-the-child-cannot-bind-is-refused-by-name")
def test_a_seed_the_child_accepts_under_another_schema_is_refused(
    seeded_schema: bytes,
) -> None:
    """The first round's value is typed by the same order every later round is.

    Only the seeded output's schema differs, so nothing else can raise this: the
    carried result still agrees with the order, and the boundary is otherwise the
    one that binds.
    """
    disagreeing = ITERATING_PARENT.replace(
        b"""      - name: first_verdict
        schema: {ref: review_verdict, revision: schema-verdict}""",
        b"""      - name: first_verdict
        schema: """
        + seeded_schema,
    )
    assert disagreeing != ITERATING_PARENT

    with pytest.raises(SubworkflowBindingRefused) as raised:
        bind_iterating(document=disagreeing)

    refusal = raised.value.refusal
    assert refusal.reason is SubworkflowBindingRefusalReason.SCHEMA_REVISION_MISMATCH
    assert refusal.node == "review_panel"
    assert "previous_verdict" in str(refusal)


@pytest.mark.proves("a-round-handover-the-child-cannot-bind-is-refused-by-name")
def test_a_seed_agreeing_with_the_order_it_fills_binds() -> None:
    """The control the refusal above is measured against."""
    binding = bind_iterating()

    assert binding.subworkflows[0].node_id == "review_panel"


@pytest.mark.proves("an-iteration-past-the-proven-round-bound-is-refused-by-name")
def test_an_iteration_declaring_more_rounds_than_were_proven_is_refused() -> None:
    with pytest.raises(SubworkflowBindingRefused) as raised:
        bind_iterating(maximum_iteration_rounds=3)

    refusal = raised.value.refusal
    assert refusal.node == "review_panel"
    assert "4" in str(refusal) and "3" in str(refusal)


@pytest.mark.proves("an-iteration-past-the-proven-round-bound-is-refused-by-name")
def test_an_iteration_within_the_proven_round_bound_binds() -> None:
    binding = bind_iterating(maximum_iteration_rounds=4)

    assert binding.subworkflows[0].node_id == "review_panel"
