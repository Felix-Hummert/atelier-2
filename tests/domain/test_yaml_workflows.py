from __future__ import annotations

from collections.abc import MutableSequence
from typing import cast

import pytest

from atelier2.adapters.yaml_workflows import (
    InvalidWorkflowDocument,
    parse_workflow_document,
)
from atelier2.contracts.workflows import AgentNode, SubworkflowNode, WorkflowGraph

VALID_DOCUMENT = b"""format_version: 1
start: agent
nodes:
  - id: final
    type: subworkflow
    operation: add
    operands: [2, 3]
    next: null
  - id: waiting
    type: wait
    answer_type: integer
    next: final
  - id: action
    type: action
    next: waiting
  - id: agent
    type: agent
    job: job-17
    output: draft-17
    next: action
"""


def test_safe_yaml_v1_preserves_list_order_but_graph_edges_own_execution_order() -> (
    None
):
    graph = parse_workflow_document(VALID_DOCUMENT)

    assert isinstance(graph, WorkflowGraph)
    assert [node.id for node in graph.nodes] == ["final", "waiting", "action", "agent"]
    assert isinstance(graph.node("agent"), AgentNode)
    assert graph.successor("agent").id == "action"


def test_validated_graph_collections_are_deeply_immutable() -> None:
    graph = parse_workflow_document(VALID_DOCUMENT)
    terminal = graph.node("final")
    assert isinstance(terminal, SubworkflowNode)

    with pytest.raises(AttributeError):
        cast(MutableSequence[object], graph.nodes).clear()
    with pytest.raises(AttributeError):
        cast(MutableSequence[int], terminal.operands).append(99)


INVALID_DOCUMENTS = {
    "invalid-utf8": b"\xff",
    "bom": b"\xef\xbb\xbfformat_version: 1\nstart: a\nnodes: []\n",
    "empty": b"",
    "multiple-documents": VALID_DOCUMENT + b"---\n{}\n",
    "duplicate-key": VALID_DOCUMENT.replace(
        b"start: agent\n", b"start: agent\nstart: agent\n"
    ),
    "alias": b"format_version: 1\nstart: a\nnodes: &nodes []\ncopy: *nodes\n",
    "merge": b"format_version: 1\nstart: a\nnodes: [{<<: {id: a}, type: agent}]\n",
    "unsafe-tag": b"!!python/object/apply:os.system ['true']\n",
    "explicit-core-tag": b"format_version: !!int 1\nstart: a\nnodes: []\n",
    "unknown-top-field": VALID_DOCUMENT + b"other: true\n",
    "unknown-node-field": VALID_DOCUMENT.replace(
        b"    next: action\n", b"    next: action\n    other: x\n"
    ),
    "bool-version": VALID_DOCUMENT.replace(
        b"format_version: 1", b"format_version: true"
    ),
    "wrong-version": VALID_DOCUMENT.replace(b"format_version: 1", b"format_version: 2"),
    "empty-start": VALID_DOCUMENT.replace(b"start: agent", b"start: ''"),
    "missing-start": VALID_DOCUMENT.replace(b"start: agent\n", b""),
    "duplicate-node": VALID_DOCUMENT.replace(
        b"  - id: action\n", b"  - id: agent\n", 1
    ),
    "missing-reference": VALID_DOCUMENT.replace(b"next: action", b"next: absent"),
    "cycle": VALID_DOCUMENT.replace(b"next: final", b"next: agent", 1),
    "unreachable": VALID_DOCUMENT.replace(
        b"nodes:\n",
        b"nodes:\n  - id: lost\n    type: wait\n    answer_type: integer\n    next: final\n",
    ),
    "second-action": VALID_DOCUMENT.replace(
        b"nodes:\n",
        b"nodes:\n  - id: other-action\n    type: action\n    next: final\n",
    ),
    "wrong-terminal": VALID_DOCUMENT.replace(
        b"type: subworkflow", b"type: wait"
    ).replace(
        b"    operation: add\n    operands: [2, 3]\n", b"    answer_type: integer\n"
    ),
    "action-as-start": VALID_DOCUMENT.replace(b"start: agent", b"start: action"),
    "empty-agent-output": VALID_DOCUMENT.replace(b"output: draft-17", b"output: ''"),
    "bool-operand": VALID_DOCUMENT.replace(b"operands: [2, 3]", b"operands: [true, 3]"),
    "unknown-node-type": VALID_DOCUMENT.replace(b"type: wait", b"type: mystery"),
    "action-predecessor-not-agent": b"""format_version: 1
start: agent
nodes:
  - {id: final, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: action, type: action, next: final}
  - {id: waiting, type: wait, answer_type: integer, next: action}
  - {id: agent, type: agent, job: job-17, output: draft-17, next: waiting}
""",
}


@pytest.mark.parametrize("document", INVALID_DOCUMENTS.values(), ids=INVALID_DOCUMENTS)
def test_every_unsafe_or_invalid_yaml_v1_form_is_rejected(document: bytes) -> None:
    with pytest.raises(InvalidWorkflowDocument):
        parse_workflow_document(document)
