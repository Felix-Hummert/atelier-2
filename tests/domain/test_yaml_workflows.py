from __future__ import annotations

from collections.abc import MutableSequence
from typing import cast

import pytest

from atelier2.adapters.yaml_workflows import (
    InvalidWorkflowDocument,
    parse_workflow_document,
)
from atelier2.contracts.workflows_v3 import WaitNodeV3, WorkflowGraphV3
from tests.scenarios.workflows import V3_WAIT_LINE_DOCUMENT

VALID_DOCUMENT = V3_WAIT_LINE_DOCUMENT


def test_the_safe_yaml_wrapper_reads_a_document_into_its_declared_format() -> None:
    graph = parse_workflow_document(VALID_DOCUMENT)

    assert isinstance(graph, WorkflowGraphV3)
    assert isinstance(graph.node("approve"), WaitNodeV3)


def test_validated_graph_collections_are_deeply_immutable() -> None:
    graph = parse_workflow_document(VALID_DOCUMENT)
    node = graph.node("approve")
    assert isinstance(node, WaitNodeV3)

    with pytest.raises(AttributeError):
        cast(MutableSequence[object], graph.nodes).clear()
    with pytest.raises(AttributeError):
        cast(MutableSequence[object], node.outputs).clear()


INVALID_DOCUMENTS = {
    "invalid-utf8": b"\xff",
    "bom": b"\xef\xbb\xbf" + VALID_DOCUMENT,
    "empty": b"",
    "multiple-documents": VALID_DOCUMENT + b"---\n{}\n",
    "duplicate-key": VALID_DOCUMENT.replace(
        b"name: A person answers, then the line is done\n",
        b"name: A person answers, then the line is done\n"
        b"name: A person answers, then the line is done\n",
    ),
    "alias": b"format_version: 3\nname: a\nnodes: &nodes []\ncopy: *nodes\n",
    "merge": b"format_version: 3\nname: a\nnodes: [{<<: {id: a}, type: wait}]\n",
    "unsafe-tag": b"!!python/object/apply:os.system ['true']\n",
    "explicit-core-tag": VALID_DOCUMENT.replace(
        b"format_version: 3", b"format_version: !!int 3"
    ),
    "unknown-top-field": VALID_DOCUMENT + b"other: true\n",
    "unknown-node-field": VALID_DOCUMENT.replace(
        b"    prompt: Approve this line.\n",
        b"    prompt: Approve this line.\n    other: x\n",
    ),
    "bool-version": VALID_DOCUMENT.replace(
        b"format_version: 3", b"format_version: true"
    ),
}


@pytest.mark.parametrize("document", INVALID_DOCUMENTS.values(), ids=INVALID_DOCUMENTS)
def test_every_unsafe_or_invalid_yaml_form_is_rejected_before_a_format_reads_it(
    document: bytes,
) -> None:
    with pytest.raises(InvalidWorkflowDocument):
        parse_workflow_document(document)


@pytest.mark.parametrize("declared", [1, 2])
def test_a_retired_document_format_is_refused_by_name(declared: int) -> None:
    """V1 and V2 stay named `WorkflowFormatVersion` members for the durable
    layer's historical rows, but no model reads a document that declares one
    (#901 slice 5): the retired key is refused, not looked up into a `KeyError`.
    """
    document = VALID_DOCUMENT.replace(
        b"format_version: 3", f"format_version: {declared}".encode()
    )

    with pytest.raises(InvalidWorkflowDocument, match="unsupported"):
        parse_workflow_document(document)
