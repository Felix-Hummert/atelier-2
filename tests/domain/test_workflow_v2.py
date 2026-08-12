from __future__ import annotations

import pytest

from atelier2.adapters.yaml_workflows import (
    InvalidWorkflowDocument,
    parse_workflow_document,
)
from atelier2.contracts.workflows import AgentNodeV2, WorkflowGraphV2

_DOCUMENT = b"""format_version: 2
start: build
nodes:
  - {id: done, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: build, type: agent, role: builder, job: implement, next: done}
"""


def test_v2_graph_names_only_role_and_job_on_agent_nodes() -> None:
    graph = parse_workflow_document(_DOCUMENT)

    assert isinstance(graph, WorkflowGraphV2)
    node = graph.node("build")
    assert isinstance(node, AgentNodeV2)
    assert node.model_dump() == {
        "id": "build",
        "type": "agent",
        "role": "builder",
        "job": "implement",
        "next": "done",
    }


@pytest.mark.parametrize(
    "forbidden", ("output", "provider", "model", "auth", "credential")
)
def test_v2_agent_rejects_every_configuration_or_fake_output_field(
    forbidden: str,
) -> None:
    document = _DOCUMENT.replace(
        b"job: implement", f"job: implement, {forbidden}: forbidden".encode()
    )

    with pytest.raises(InvalidWorkflowDocument):
        parse_workflow_document(document)


@pytest.mark.parametrize(
    "document",
    (
        _DOCUMENT.replace(b"format_version: 2", b"format_version: 3"),
        _DOCUMENT.replace(b"format_version: 2", b"format_version: true"),
        _DOCUMENT.replace(b"role: builder", b"output: fake"),
    ),
)
def test_version_dispatch_rejects_unknown_and_mixed_graphs(document: bytes) -> None:
    with pytest.raises(InvalidWorkflowDocument):
        parse_workflow_document(document)
