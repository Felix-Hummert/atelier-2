from __future__ import annotations

import pytest

from atelier2.adapters.dbos.run_transitions import (
    RunTransitionConflict,
    validate_run_graph_binding,
)
from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.contracts.agents import AgentBindingSet
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.run_bindings import RunV3
from atelier2.contracts.run_configuration_v3 import RunConfigurationRevisionHash
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
from atelier2.contracts.workflows import RunCompletes, completion_after_node
from atelier2.contracts.workflows_v3 import (
    BranchingAdvanceUnsupported,
    MultipleSinkCompletionUnsupported,
    WorkflowGraphV3,
    is_sink_node,
)
from tests.domain.test_workflow_v3 import graph as v3_graph
from tests.scenarios.workflows import (
    ANY_JSON_SCHEMA,
    V3_WAIT_LINE_DOCUMENT,
    V3_WAIT_LINE_NODE_ID,
    declared_output,
)

_MULTI_SINK_V3_DOCUMENT = b"""format_version: 3
name: two independent exits
nodes:
  - id: first
    type: agent
    role: builder
    mode: headless
    instruction: build
  - id: second
    type: agent
    role: reviewer
    mode: headless
    instruction: review
"""

_TWO_WAIT_NODE_DOCUMENT = (
    b"""format_version: 3
name: two waits, one line
nodes:
  - id: first
    type: wait
    prompt: first
"""
    + declared_output(ANY_JSON_SCHEMA, "first_answer")
    + b"""  - id: second
    type: wait
    prompt: second
    depends_on: [first]
"""
    + declared_output(ANY_JSON_SCHEMA, "second_answer")
)

_NO_AGENT_BINDINGS = AgentBindingSet(())
_RUN_CONFIGURATION_REVISION_HASH = RunConfigurationRevisionHash("0" * 64)


def _single_node_graph() -> WorkflowGraphV3:
    graph = parse_workflow_document(V3_WAIT_LINE_DOCUMENT)
    assert isinstance(graph, WorkflowGraphV3)
    return graph


def _two_node_graph() -> WorkflowGraphV3:
    graph = parse_workflow_document(_TWO_WAIT_NODE_DOCUMENT)
    assert isinstance(graph, WorkflowGraphV3)
    return graph


def _completed_run(document: bytes, node_id: str) -> RunV3:
    return RunV3(
        RunId("run-1"),
        WorkflowRevision(document).revision_hash,
        _NO_AGENT_BINDINGS.binding_set_hash,
        (),
        RunState.COMPLETED,
        node_id,
        2,
        4,
        _RUN_CONFIGURATION_REVISION_HASH,
        Sha256Hash.of(b"terminal"),
    )


def test_v3_multi_sink_completion_is_refused_until_all_sinks_have_an_owner() -> None:
    graph = parse_workflow_document(_MULTI_SINK_V3_DOCUMENT)

    with pytest.raises(MultipleSinkCompletionUnsupported) as refused:
        is_sink_node(graph, "first")

    assert refused.value.sink_node_ids == ("first", "second")


@pytest.mark.proves("a-completed-v3-attempt-reaches-the-runs-terminal-hash")
def test_a_v3_sink_completes_its_run_and_a_non_sink_is_refused_by_name() -> None:
    """The V3 halves this cut answers, and the one it still declines to decide.

    A finished V3 node used to reach for a V1 spelling that is not there, which
    raised `AttributeError` from inside a durable transition. The sink half is
    answered here, and so is the line: after a node with exactly one declared
    heir, that heir is next. Where a node has several dependents, choosing
    between them is the ready set's decision, and it is refused in typed words
    rather than guessed.
    """
    graph = v3_graph()
    sink = graph.sink_node_ids[0]
    branching = next(
        node.id
        for node in graph.nodes
        if len([other for other in graph.nodes if node.id in other.depends_on]) > 1
    )

    assert completion_after_node(graph, sink) == RunCompletes()

    with pytest.raises(BranchingAdvanceUnsupported) as refused:
        completion_after_node(graph, branching)

    assert refused.value.node_id == branching
    assert len(refused.value.dependents) > 1


def test_a_reentered_completed_run_must_stand_on_the_sink() -> None:
    # Every re-entry after a crash reloads the run through this rule, so it is
    # what decides whether a durable COMPLETED head is coherent at all.
    graph = _two_node_graph()

    validate_run_graph_binding(
        _completed_run(_TWO_WAIT_NODE_DOCUMENT, graph.sink_node_ids[0]), graph
    )

    with pytest.raises(RunTransitionConflict):
        validate_run_graph_binding(
            _completed_run(_TWO_WAIT_NODE_DOCUMENT, "first"), graph
        )


def test_a_durable_run_head_absent_from_its_graph_is_a_transition_conflict() -> None:
    graph = _single_node_graph()

    with pytest.raises(
        RunTransitionConflict,
        match="run current node is absent from its workflow graph",
    ):
        validate_run_graph_binding(
            _completed_run(V3_WAIT_LINE_DOCUMENT, "not-in-the-graph"), graph
        )


def test_a_one_node_document_completes_on_its_only_node() -> None:
    graph = _single_node_graph()

    assert graph.sink_node_ids == (V3_WAIT_LINE_NODE_ID,)
    validate_run_graph_binding(
        _completed_run(V3_WAIT_LINE_DOCUMENT, V3_WAIT_LINE_NODE_ID), graph
    )
