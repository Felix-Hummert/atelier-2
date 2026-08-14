from __future__ import annotations

from collections.abc import MutableSequence
from pathlib import Path
from typing import cast

import pytest

from atelier2.adapters.yaml_workflows import (
    FORMAT_V3_NOT_EXECUTABLE,
    InvalidWorkflowDocument,
    parse_executable_workflow_document,
    parse_workflow_document,
)
from atelier2.contracts.workflow_refusals import WorkflowRefusalReason
from atelier2.contracts.workflows_v3 import (
    MAXIMUM_INSTRUCTION_BYTES,
    RETIRED_KEY_REPLACEMENTS,
    ActionNodeV3,
    AgentNodeV3,
    ContextEntrySource,
    DeterministicNodeV3,
    NodeOutputSource,
    NodeReceiptSource,
    SubworkflowNodeV3,
    WaitNodeV3,
    WorkflowGraphV3,
)

DOCUMENT = b"""format_version: 3
graph_inputs:
  - name: brief
    schema: {ref: work_brief, revision: schema-brief}
graph_outputs:
  - name: landed
    from: {node: publish_report, output: receipt}
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: |
      Implement every acceptance sentence of the bound story.
    profile: {ref: builder_method, revision: profile-1}
    skills:
      - {ref: workspace_discipline, revision: skill-1}
    tools:
      - {ref: repository_write, revision: grant-1}
    policy: {ref: house_rules, revision: policy-1}
    budget: {ref: build_budget, revision: budget-1}
    retry: {ref: twice, revision: retry-1}
    cancellation: {ref: drain, revision: cancel-1}
    required_context:
      - name: story
        source: {ref: requirement, revision: requirement-1, selector: story_acceptance}
    available_context:
      - name: decision_records
        source: {ref: decision_record_index, revision: index-1}
        read_operations:
          - {ref: search, revision: read-1}
    inputs:
      - name: story_text
        from: {context: story}
      - name: label
        value: needs-review
    outputs:
      - name: candidate
        schema: {ref: workspace_candidate, revision: schema-candidate}
  - id: code_review
    type: agent
    role: reviewer
    mode: headless
    instruction: Name every defect with the sentence it violates.
    depends_on: [implement]
    inputs:
      - name: candidate
        from: {node: implement, output: candidate}
    outputs:
      - name: findings
        schema: {ref: review_verdict, revision: schema-verdict}
  - id: rehearse
    type: subworkflow
    depends_on: [implement]
    workflow: {ref: review_panel, revision: workflow-1}
    budget: {ref: child_budget, revision: budget-2}
    inputs:
      - name: candidate
        from: {node: implement, output: candidate}
    outputs:
      - name: rehearsal
        schema: {ref: review_verdict, revision: schema-verdict}
  - id: approve
    type: wait
    depends_on: [code_review]
    prompt: Approve this candidate, or reject it naming the blocking defect.
    required_context:
      - name: story
        source: {ref: requirement, revision: requirement-1, selector: story_acceptance}
    inputs:
      - name: findings
        from: {node: code_review, output: findings}
    outputs:
      - name: approval
        schema: {ref: landing_approval, revision: schema-approval}
  - id: merge_findings
    type: deterministic
    depends_on: [approve, rehearse]
    join: all_terminal
    operation: {ref: merge_review_verdicts, revision: operation-1}
    retry: {ref: twice, revision: retry-1}
    inputs:
      - name: approval
        from: {node: approve, output: approval}
      - name: review_outcome
        from: {node: code_review, receipt: terminal}
    outputs:
      - name: merged
        schema: {ref: review_verdict, revision: schema-verdict}
  - id: publish_report
    type: action
    depends_on: [merge_findings]
    operation: {ref: requirement_comment, revision: operation-2}
    inputs:
      - name: body
        from: {node: merge_findings, output: merged}
    outputs:
      - name: receipt
        schema: {ref: comment_receipt, revision: schema-receipt}
"""


def graph() -> WorkflowGraphV3:
    parsed = parse_workflow_document(DOCUMENT)
    assert isinstance(parsed, WorkflowGraphV3)
    return parsed


def with_node_line(node_id: str, line: str, document: bytes = DOCUMENT) -> bytes:
    anchor = f"  - id: {node_id}\n".encode()
    assert document.count(anchor) == 1
    return document.replace(anchor, anchor + f"    {line}\n".encode())


def without_line(line: str, document: bytes = DOCUMENT) -> bytes:
    return document.replace(f"{line}\n".encode(), b"", 1)


def test_every_node_kind_of_the_record_parses_into_its_own_closed_model() -> None:
    parsed = graph()

    assert [type(node) for node in parsed.nodes] == [
        AgentNodeV3,
        AgentNodeV3,
        SubworkflowNodeV3,
        WaitNodeV3,
        DeterministicNodeV3,
        ActionNodeV3,
    ]
    builder = parsed.node("implement")
    assert isinstance(builder, AgentNodeV3)
    assert (builder.role, builder.mode) == ("builder", "headless")
    assert builder.instruction.startswith("Implement every acceptance sentence")
    assert builder.profile is not None and builder.profile.revision == "profile-1"
    assert [skill.ref for skill in builder.skills] == ["workspace_discipline"]
    assert [tool.ref for tool in builder.tools] == ["repository_write"]
    assert builder.policy is not None and builder.policy.ref == "house_rules"
    assert builder.budget is not None and builder.budget.ref == "build_budget"
    assert builder.retry is not None and builder.retry.ref == "twice"
    assert builder.cancellation is not None and builder.cancellation.ref == "drain"
    assert builder.required_context[0].source.selector == "story_acceptance"
    assert [
        operation.ref for operation in builder.available_context[0].read_operations
    ] == ["search"]
    assert builder.outputs[0].schema_reference.revision == "schema-candidate"


def test_the_four_input_sources_carry_their_declared_shape() -> None:
    parsed = graph()
    builder = parsed.node("implement")
    reviewer = parsed.node("code_review")
    merge = parsed.node("merge_findings")

    assert isinstance(builder.inputs[0].source, ContextEntrySource)
    assert builder.inputs[1].source is None
    assert builder.inputs[1].value == "needs-review"
    assert isinstance(reviewer.inputs[0].source, NodeOutputSource)
    assert isinstance(merge.inputs[1].source, NodeReceiptSource)
    assert merge.inputs[1].source.node == "code_review"


def test_control_edges_alone_derive_the_entry_sink_and_dependency_sets() -> None:
    parsed = graph()

    assert parsed.entry_node_ids == ("implement",)
    assert parsed.sink_node_ids == ("publish_report",)
    assert parsed.dependency_closure("merge_findings") == frozenset(
        {"implement", "code_review", "rehearse", "approve"}
    )
    assert parsed.graph_inputs[0].schema_reference.ref == "work_brief"
    assert parsed.graph_outputs[0].source.node == "publish_report"


def test_a_wait_declares_its_prompt_and_exactly_one_answer_schema() -> None:
    waiting = graph().node("approve")

    assert isinstance(waiting, WaitNodeV3)
    assert waiting.prompt.startswith("Approve this candidate")
    assert len(waiting.outputs) == 1
    assert waiting.outputs[0].schema_reference.ref == "landing_approval"


def test_a_subworkflow_declares_the_child_revision_it_reuses() -> None:
    child = graph().node("rehearse")

    assert isinstance(child, SubworkflowNodeV3)
    assert child.workflow.ref == "review_panel"
    assert [output.name for output in child.outputs] == ["rehearsal"]


@pytest.mark.parametrize(
    ("node_id", "expected"),
    [
        ("implement", None),
        ("code_review", "all_succeeded"),
        ("merge_findings", "all_terminal"),
    ],
    ids=["no-dependency", "single-dependency-default", "fan-in"],
)
def test_the_omitted_single_dependency_join_is_all_succeeded(
    node_id: str, expected: str | None
) -> None:
    assert graph().join_of(node_id) == expected


def test_writing_the_default_join_out_over_one_dependency_means_the_same() -> None:
    spelled_out = parse_workflow_document(
        with_node_line("code_review", "join: all_succeeded")
    )

    assert isinstance(spelled_out, WorkflowGraphV3)
    assert spelled_out.join_of("code_review") == graph().join_of("code_review")


def test_all_terminal_stays_authorable_over_a_single_dependency() -> None:
    parsed = parse_workflow_document(with_node_line("approve", "join: all_terminal"))

    assert isinstance(parsed, WorkflowGraphV3)
    assert parsed.join_of("approve") == "all_terminal"


def test_a_parsed_v3_document_is_deeply_immutable() -> None:
    parsed = graph()

    with pytest.raises(AttributeError):
        cast(MutableSequence[object], parsed.nodes).clear()
    with pytest.raises(ValueError, match="frozen"):
        parsed.node("implement").id = "renamed"


REFUSALS: dict[str, tuple[bytes, WorkflowRefusalReason, str | None, str]] = {
    "unknown-document-field": (
        DOCUMENT + b"owner: felix\n",
        WorkflowRefusalReason.UNKNOWN_FIELD,
        None,
        "owner",
    ),
    "unknown-node-field": (
        with_node_line("implement", "surprise: yes"),
        WorkflowRefusalReason.UNKNOWN_FIELD,
        "implement",
        "surprise",
    ),
    "budget-refused-by-deterministic": (
        with_node_line("merge_findings", "budget: {ref: b, revision: r}"),
        WorkflowRefusalReason.REFUSED_FIELD,
        "merge_findings",
        "budget",
    ),
    "available-context-refused-by-wait": (
        with_node_line("approve", "available_context: []"),
        WorkflowRefusalReason.REFUSED_FIELD,
        "approve",
        "available_context",
    ),
    "retry-refused-by-action": (
        with_node_line("publish_report", "retry: {ref: twice, revision: retry-1}"),
        WorkflowRefusalReason.REFUSED_FIELD,
        "publish_report",
        "retry",
    ),
    "instruction-refused-by-deterministic": (
        with_node_line("merge_findings", "instruction: do the work"),
        WorkflowRefusalReason.REFUSED_FIELD,
        "merge_findings",
        "instruction",
    ),
    "required-context-refused-by-subworkflow": (
        with_node_line("rehearse", "required_context: []"),
        WorkflowRefusalReason.REFUSED_FIELD,
        "rehearse",
        "required_context",
    ),
    "prompt-refused-by-agent": (
        with_node_line("implement", "prompt: decide"),
        WorkflowRefusalReason.REFUSED_FIELD,
        "implement",
        "prompt",
    ),
    "missing-mode": (
        without_line("    mode: headless"),
        WorkflowRefusalReason.MISSING_FIELD,
        "implement",
        "mode",
    ),
    "missing-operation": (
        without_line(
            "    operation: {ref: requirement_comment, revision: operation-2}"
        ),
        WorkflowRefusalReason.MISSING_FIELD,
        "publish_report",
        "operation",
    ),
    "unpinned-reference": (
        DOCUMENT.replace(
            b"profile: {ref: builder_method, revision: profile-1}",
            b"profile: {ref: builder_method}",
        ),
        WorkflowRefusalReason.MISSING_FIELD,
        "implement",
        "revision",
    ),
    "empty-instruction": (
        DOCUMENT.replace(
            b"instruction: Name every defect with the sentence it violates.",
            b"instruction: ' '",
        ),
        WorkflowRefusalReason.INVALID_VALUE,
        "code_review",
        "instruction",
    ),
    "oversized-instruction": (
        DOCUMENT.replace(
            b"instruction: Name every defect with the sentence it violates.",
            b"instruction: " + b"x" * (MAXIMUM_INSTRUCTION_BYTES + 1),
        ),
        WorkflowRefusalReason.INVALID_VALUE,
        "code_review",
        "instruction",
    ),
    "wait-without-exactly-one-output": (
        DOCUMENT.replace(
            b"      - name: approval\n"
            b"        schema: {ref: landing_approval, revision: schema-approval}\n",
            b"      - name: approval\n"
            b"        schema: {ref: landing_approval, revision: schema-approval}\n"
            b"      - name: second\n"
            b"        schema: {ref: landing_approval, revision: schema-approval}\n",
        ),
        WorkflowRefusalReason.INVALID_VALUE,
        "approve",
        "outputs",
    ),
    "grant-without-read-operation": (
        DOCUMENT.replace(
            b"        read_operations:\n          - {ref: search, revision: read-1}\n",
            b"        read_operations: []\n",
        ),
        WorkflowRefusalReason.INVALID_VALUE,
        "implement",
        "read_operations",
    ),
    "unknown-node-kind": (
        DOCUMENT.replace(b"    type: wait\n", b"    type: mystery\n"),
        WorkflowRefusalReason.INVALID_VALUE,
        "approve",
        "nodes",
    ),
    "no-nodes": (
        b"format_version: 3\nnodes: []\n",
        WorkflowRefusalReason.INVALID_VALUE,
        None,
        "nodes",
    ),
    "input-without-a-source": (
        DOCUMENT.replace(b"        from: {context: story}\n", b""),
        WorkflowRefusalReason.INVALID_VALUE,
        "implement",
        "inputs",
    ),
    "input-with-two-sources": (
        DOCUMENT.replace(
            b"      - name: label\n        value: needs-review\n",
            b"      - name: label\n"
            b"        value: needs-review\n"
            b"        from: {context: story}\n",
        ),
        WorkflowRefusalReason.INVALID_VALUE,
        "implement",
        "inputs",
    ),
    "input-source-in-no-declared-form": (
        DOCUMENT.replace(
            b"        from: {node: implement, output: candidate}\n",
            b"        from: {node: implement}\n",
            1,
        ),
        WorkflowRefusalReason.INVALID_VALUE,
        "code_review",
        "from",
    ),
    "duplicate-node-id": (
        DOCUMENT.replace(b"  - id: rehearse\n", b"  - id: code_review\n"),
        WorkflowRefusalReason.DUPLICATE_NODE_ID,
        "code_review",
        "id",
    ),
    "duplicate-input-name": (
        DOCUMENT.replace(
            b"      - name: review_outcome\n", b"      - name: approval\n"
        ),
        WorkflowRefusalReason.DUPLICATE_NAME,
        "merge_findings",
        "inputs",
    ),
    "duplicate-output-name": (
        DOCUMENT.replace(
            b"      - name: candidate\n"
            b"        schema: {ref: workspace_candidate, revision: schema-candidate}\n",
            b"      - name: candidate\n"
            b"        schema: {ref: workspace_candidate, revision: schema-candidate}\n"
            b"      - name: candidate\n"
            b"        schema: {ref: workspace_candidate, revision: schema-candidate}\n",
        ),
        WorkflowRefusalReason.DUPLICATE_NAME,
        "implement",
        "outputs",
    ),
    "duplicate-dependency": (
        DOCUMENT.replace(
            b"    depends_on: [approve, rehearse]\n",
            b"    depends_on: [approve, approve]\n",
        ),
        WorkflowRefusalReason.DUPLICATE_NAME,
        "merge_findings",
        "depends_on",
    ),
    "duplicate-graph-output-name": (
        DOCUMENT.replace(
            b"  - name: landed\n    from: {node: publish_report, output: receipt}\n",
            b"  - name: landed\n    from: {node: publish_report, output: receipt}\n"
            b"  - name: landed\n    from: {node: publish_report, output: receipt}\n",
        ),
        WorkflowRefusalReason.DUPLICATE_NAME,
        None,
        "graph_outputs",
    ),
    "unknown-dependency": (
        DOCUMENT.replace(
            b"    depends_on: [code_review]\n", b"    depends_on: [gone]\n"
        ),
        WorkflowRefusalReason.UNKNOWN_NODE_REFERENCE,
        "approve",
        "depends_on",
    ),
    "self-dependency": (
        DOCUMENT.replace(
            b"    depends_on: [code_review]\n", b"    depends_on: [approve]\n"
        ),
        WorkflowRefusalReason.CYCLE,
        "approve",
        "depends_on",
    ),
    "cycle": (
        with_node_line("implement", "depends_on: [publish_report]"),
        WorkflowRefusalReason.CYCLE,
        "implement",
        "depends_on",
    ),
    "join-without-dependency": (
        with_node_line("implement", "join: all_succeeded"),
        WorkflowRefusalReason.JOIN_WITHOUT_DEPENDENCY,
        "implement",
        "join",
    ),
    "join-missing-on-fan-in": (
        without_line("    join: all_terminal"),
        WorkflowRefusalReason.JOIN_REQUIRED,
        "merge_findings",
        "join",
    ),
    "data-edge-outside-the-dependency-closure": (
        DOCUMENT.replace(
            b"        from: {node: implement, output: candidate}\n",
            b"        from: {node: approve, output: approval}\n",
            1,
        ),
        WorkflowRefusalReason.DATA_EDGE_OUTSIDE_CLOSURE,
        "code_review",
        "inputs",
    ),
    "data-edge-to-an-undeclared-output": (
        DOCUMENT.replace(
            b"        from: {node: implement, output: candidate}\n",
            b"        from: {node: implement, output: absent}\n",
            1,
        ),
        WorkflowRefusalReason.UNDECLARED_OUTPUT,
        "code_review",
        "inputs",
    ),
    "input-reading-an-undeclared-context": (
        DOCUMENT.replace(
            b"        from: {context: story}\n", b"        from: {context: absent}\n"
        ),
        WorkflowRefusalReason.UNDECLARED_CONTEXT,
        "implement",
        "inputs",
    ),
    "graph-output-from-a-non-sink": (
        DOCUMENT.replace(
            b"    from: {node: publish_report, output: receipt}\n",
            b"    from: {node: implement, output: candidate}\n",
        ),
        WorkflowRefusalReason.GRAPH_OUTPUT_NOT_SINK,
        None,
        "graph_outputs",
    ),
    "graph-output-from-an-unknown-node": (
        DOCUMENT.replace(
            b"    from: {node: publish_report, output: receipt}\n",
            b"    from: {node: gone, output: receipt}\n",
        ),
        WorkflowRefusalReason.UNKNOWN_NODE_REFERENCE,
        None,
        "graph_outputs",
    ),
    "graph-output-of-an-undeclared-output": (
        DOCUMENT.replace(
            b"    from: {node: publish_report, output: receipt}\n",
            b"    from: {node: publish_report, output: absent}\n",
        ),
        WorkflowRefusalReason.UNDECLARED_OUTPUT,
        None,
        "graph_outputs",
    ),
    "interactive-output-without-operator-confirmation": (
        DOCUMENT.replace(b"    mode: headless\n", b"    mode: interactive\n", 1),
        WorkflowRefusalReason.UNCONFIRMED_INTERACTIVE_OUTPUT,
        "implement",
        "outputs",
    ),
    "operator-confirmation-without-interactive-mode": (
        DOCUMENT.replace(
            b"      - name: receipt\n",
            b"      - name: receipt\n        confirmed_by: operator\n",
        ),
        WorkflowRefusalReason.CONFIRMATION_WITHOUT_INTERACTIVE_MODE,
        "publish_report",
        "outputs",
    ),
}


@pytest.mark.parametrize(
    ("document", "reason", "node", "field"), REFUSALS.values(), ids=REFUSALS
)
def test_every_refused_v3_form_names_its_node_and_field(
    document: bytes,
    reason: WorkflowRefusalReason,
    node: str | None,
    field: str,
) -> None:
    with pytest.raises(InvalidWorkflowDocument) as raised:
        parse_workflow_document(document)

    refusal = raised.value.refusal
    assert refusal is not None
    assert (refusal.reason, refusal.node, refusal.field) == (reason, node, field)


@pytest.mark.parametrize("retired", RETIRED_KEY_REPLACEMENTS)
def test_every_retired_v1_or_v2_key_is_refused_naming_its_replacement(
    retired: str,
) -> None:
    document = with_node_line("implement", f"{retired}: carried-over")

    with pytest.raises(InvalidWorkflowDocument) as raised:
        parse_workflow_document(document)

    refusal = raised.value.refusal
    assert refusal is not None
    assert refusal.reason is WorkflowRefusalReason.RETIRED_KEY
    assert (refusal.node, refusal.field) == ("implement", retired)
    assert RETIRED_KEY_REPLACEMENTS[retired] in refusal.detail


def test_a_retired_key_is_named_at_the_document_level_too() -> None:
    with pytest.raises(InvalidWorkflowDocument) as raised:
        parse_workflow_document(DOCUMENT + b"start: implement\n")

    refusal = raised.value.refusal
    assert refusal is not None
    assert (refusal.reason, refusal.node, refusal.field) == (
        WorkflowRefusalReason.RETIRED_KEY,
        None,
        "start",
    )


UNSAFE_V3_DOCUMENTS = {
    "alias": b"format_version: 3\nnodes: &nodes []\ncopy: *nodes\n",
    "merge": b"format_version: 3\nnodes: [{<<: {id: a}, type: agent}]\n",
    "explicit-tag": b"format_version: !!int 3\nnodes: []\n",
    "bom": b"\xef\xbb\xbfformat_version: 3\nnodes: []\n",
    "duplicate-key": DOCUMENT + b"format_version: 3\n",
    "multiple-documents": DOCUMENT + b"---\n{}\n",
}


@pytest.mark.parametrize(
    "document", UNSAFE_V3_DOCUMENTS.values(), ids=UNSAFE_V3_DOCUMENTS
)
def test_unsafe_yaml_is_refused_before_any_v3_vocabulary_is_read(
    document: bytes,
) -> None:
    with pytest.raises(InvalidWorkflowDocument) as raised:
        parse_workflow_document(document)

    assert raised.value.refusal is None


def test_a_v3_document_parses_but_no_runtime_executes_it() -> None:
    with pytest.raises(InvalidWorkflowDocument, match=FORMAT_V3_NOT_EXECUTABLE):
        parse_executable_workflow_document(DOCUMENT)


def test_the_worked_example_of_the_record_parses_unchanged() -> None:
    record = Path(__file__).parents[2] / "docs/decisions/0006-node-vocabulary.md"
    text = record.read_text(encoding="utf-8")
    example = text.split("## Worked example", 1)[1].split("```yaml\n", 1)[1]

    parsed = parse_workflow_document(example.split("```", 1)[0].encode("utf-8"))

    assert isinstance(parsed, WorkflowGraphV3)
    assert parsed.entry_node_ids == ("implement",)
    assert parsed.join_of("merge_findings") == "all_terminal"
