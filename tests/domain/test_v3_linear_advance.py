from __future__ import annotations

import pytest

from atelier2.adapters.yaml_workflows import (
    WorkflowFormatNotExecutable,
    parse_executable_workflow_document,
    parse_workflow_document,
)
from atelier2.contracts.workflows import (
    RunCompletes,
    RunContinues,
    completion_after_node,
)
from atelier2.contracts.workflows_v3 import (
    BranchingAdvanceUnsupported,
    WorkflowGraphV3,
)

_ONE_NODE = b"""format_version: 3
name: One agent
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Do the one thing.
"""

_LINE = (
    _ONE_NODE
    + b"""  - id: review
    type: agent
    role: reviewer
    mode: headless
    instruction: Judge the first thing.
    depends_on: [implement]
"""
)

_FAN_OUT = (
    _LINE
    + b"""  - id: document
    type: agent
    role: writer
    mode: headless
    instruction: Write it up.
    depends_on: [implement]
"""
)

_DIAMOND = (
    _FAN_OUT
    + b"""  - id: ship
    type: agent
    role: shipper
    mode: headless
    instruction: Ship it.
    depends_on: [review, document]
    join: all_succeeded
"""
)

_FAN_IN = (
    _ONE_NODE
    + b"""  - id: research
    type: agent
    role: researcher
    mode: headless
    instruction: Do the other first thing.
  - id: review
    type: agent
    role: reviewer
    mode: headless
    instruction: Judge both.
    depends_on: [implement, research]
    join: all_succeeded
"""
)


def _graph(document: bytes) -> WorkflowGraphV3:
    graph = parse_workflow_document(document)
    assert isinstance(graph, WorkflowGraphV3)
    return graph


@pytest.mark.proves("a-completed-v3-attempt-reaches-the-runs-terminal-hash")
def test_after_a_v3_node_its_one_declared_heir_is_next() -> None:
    graph = _graph(_LINE)

    assert completion_after_node(graph, "implement") == RunContinues("review")
    assert completion_after_node(graph, "review") == RunCompletes()


@pytest.mark.proves("every-v3-shape-no-runtime-binds-is-refused-by-name")
def test_a_line_of_agent_nodes_is_executable() -> None:
    graph = parse_executable_workflow_document(_LINE)

    assert isinstance(graph, WorkflowGraphV3)
    assert graph.sink_node_ids == ("review",)


@pytest.mark.proves("every-v3-shape-no-runtime-binds-is-refused-by-name")
@pytest.mark.parametrize("document", [_FAN_OUT, _FAN_IN, _DIAMOND])
def test_a_branching_v3_document_stays_refused_for_the_ready_set(
    document: bytes,
) -> None:
    with pytest.raises(WorkflowFormatNotExecutable) as refused:
        parse_executable_workflow_document(document)

    assert "ready set" in str(refused.value)


@pytest.mark.proves("a-completed-v3-attempt-reaches-the-runs-terminal-hash")
def test_a_branching_v3_graph_never_invents_a_single_successor() -> None:
    # The diamond has one entry and one sink, so nothing else refuses first:
    # what must refuse is the advance rule itself, naming the branch.
    with pytest.raises(BranchingAdvanceUnsupported) as refused:
        completion_after_node(_graph(_DIAMOND), "implement")

    assert refused.value.node_id == "implement"
    assert set(refused.value.dependents) == {"review", "document"}


@pytest.mark.proves("a-completed-v3-attempt-reaches-the-runs-terminal-hash")
def test_a_node_is_never_advanced_into_before_its_other_dependency_ran() -> None:
    # `research` has exactly one dependent, so the branch check upstream never
    # fires -- but that heir also waits on `implement`. Advancing there would
    # start a node whose other dependency has not run, which is the join the
    # ready set owns.
    with pytest.raises(BranchingAdvanceUnsupported) as refused:
        completion_after_node(_graph(_FAN_IN), "research")

    assert refused.value.node_id == "review"
    assert set(refused.value.dependents) == {"implement", "research"}
