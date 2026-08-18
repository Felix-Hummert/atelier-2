from __future__ import annotations

import pytest

from atelier2.adapters.yaml_workflows import (
    WorkflowFormatNotExecutable,
    parse_executable_workflow_document,
    parse_workflow_document,
)
from atelier2.contracts.runs import FIRST_ROUND_ORDINAL
from atelier2.contracts.workflows import (
    RunCompletes,
    RunContinues,
    completion_after_node,
)
from atelier2.contracts.workflows_v3 import (
    BranchingAdvanceUnsupported,
    WorkflowGraphV3,
)

_ANY_JSON_REVISION = "e" * 64
_DECLARED_OUTPUT = f"""    outputs:
      - name: result
        schema: {{ref: any-json, revision: "{_ANY_JSON_REVISION}"}}
""".encode()
"""The one output every executable agent node declares, as `single-json-output/v1`.

These documents are parsed, never run, so the revision it pins is a placeholder
of the right shape: what is under test is the form the executable admission
requires, not the schema a run would resolve.
"""

_ONE_NODE = (
    b"""format_version: 3
name: One agent
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Do the one thing.
"""
    + _DECLARED_OUTPUT
)

_LINE = (
    _ONE_NODE
    + b"""  - id: review
    type: agent
    role: reviewer
    mode: headless
    instruction: Judge the first thing.
    depends_on: [implement]
"""
    + _DECLARED_OUTPUT
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
    + _DECLARED_OUTPUT
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
    + _DECLARED_OUTPUT
)

_FAN_IN = (
    _ONE_NODE
    + b"""  - id: research
    type: agent
    role: researcher
    mode: headless
    instruction: Do the other first thing.
"""
    + _DECLARED_OUTPUT
    + b"""  - id: review
    type: agent
    role: reviewer
    mode: headless
    instruction: Judge both.
    depends_on: [implement, research]
    join: all_succeeded
"""
    + _DECLARED_OUTPUT
)


_LOOPED_LINE = (
    _LINE
    + b"""loops:
  - id: until_reviewed
    body: [implement, review]
    maximum_rounds: 3
"""
)

_LOOP_BEFORE_A_TAIL = (
    _LINE
    + b"""  - id: ship
    type: agent
    role: shipper
    mode: headless
    instruction: Ship what came out.
    depends_on: [review]
"""
    + _DECLARED_OUTPUT
    + b"""loops:
  - id: until_reviewed
    body: [implement, review]
    maximum_rounds: 2
"""
)


_LOOPED_WAIT = (
    _ONE_NODE
    + b"""  - id: approve
    type: wait
    prompt: Is this good enough?
    depends_on: [implement]
"""
    + _DECLARED_OUTPUT
    + b"""loops:
  - id: until_approved
    body: [implement, approve]
    maximum_rounds: 2
"""
)

_READ_OUT_OF_A_LOOP = (
    _LINE
    + b"""  - id: ship
    type: agent
    role: shipper
    mode: headless
    instruction: Ship what came out.
    depends_on: [review]
    inputs:
      - name: verdict
        from: {node: review, output: result}
"""
    + _DECLARED_OUTPUT
    + b"""loops:
  - id: until_reviewed
    body: [implement, review]
    maximum_rounds: 2
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


@pytest.mark.proves("a-declared-loop-runs-its-rounds-and-ends-at-its-bound")
def test_the_last_node_of_a_round_hands_back_to_the_first_while_rounds_remain() -> None:
    graph = _graph(_LOOPED_LINE)

    assert completion_after_node(graph, "review", 1) == RunContinues("implement", 2)
    assert completion_after_node(graph, "review", 2) == RunContinues("implement", 3)


@pytest.mark.proves("a-declared-loop-runs-its-rounds-and-ends-at-its-bound")
def test_inside_a_round_the_advance_is_the_line_it_always_was() -> None:
    graph = _graph(_LOOPED_LINE)

    assert completion_after_node(graph, "implement", 2) == RunContinues("review", 2)


@pytest.mark.proves("a-declared-loop-runs-its-rounds-and-ends-at-its-bound")
def test_the_declared_bound_is_the_only_way_out_of_a_loop_this_build_has() -> None:
    """No result ends it early: the round count is what the run answers to."""
    graph = _graph(_LOOPED_LINE)

    assert completion_after_node(graph, "review", 3) == RunCompletes()


@pytest.mark.proves("a-declared-loop-runs-its-rounds-and-ends-at-its-bound")
def test_a_node_after_a_loop_is_entered_in_the_first_round_it_has() -> None:
    """Leaving the loop leaves its rounds behind rather than carrying a count on."""
    graph = _graph(_LOOP_BEFORE_A_TAIL)

    assert completion_after_node(graph, "review", 2) == RunContinues(
        "ship", FIRST_ROUND_ORDINAL
    )
    assert completion_after_node(graph, "ship") == RunCompletes()


@pytest.mark.proves("every-v3-shape-no-runtime-binds-is-refused-by-name")
def test_a_line_a_loop_repeats_is_executable() -> None:
    graph = parse_executable_workflow_document(_LOOPED_LINE)

    assert isinstance(graph, WorkflowGraphV3)
    assert graph.declared_rounds_of("implement") == range(1, 4)
    assert graph.declared_rounds_of("review") == range(1, 4)


@pytest.mark.proves("every-v3-shape-no-runtime-binds-is-refused-by-name")
@pytest.mark.parametrize(
    ("document", "named"),
    (
        (_LOOPED_WAIT, "no round repeats"),
        (_READ_OUT_OF_A_LOOP, "which round it reads"),
    ),
    ids=("a wait node inside a loop body", "a value read out of a loop"),
)
def test_a_loop_shape_no_round_of_this_build_carries_is_refused_by_name(
    document: bytes, named: str
) -> None:
    """Both would run: what neither has is an owner for the round it means."""
    with pytest.raises(WorkflowFormatNotExecutable) as refused:
        parse_executable_workflow_document(document)

    assert named in str(refused.value)
