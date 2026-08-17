from __future__ import annotations

import pytest

from atelier2.adapters.dbos.run_store import (
    RunTransitionConflict,
    validate_run_graph_binding,
)
from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.runs import (
    Run,
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.contracts.workflows import (
    AnyWorkflowGraph,
    RunCompletes,
    RunContinues,
    WorkflowGraph,
    WorkflowGraphV2,
    completion_after_node,
)
from atelier2.contracts.workflows_v3 import (
    MultipleSinkCompletionUnsupported,
    is_sink_node,
)
from tests.domain.test_workflow_v3 import graph as v3_graph

_V1_DOCUMENT = b"""format_version: 1
start: agent
nodes:
  - {id: final, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: waiting, type: wait, answer_type: integer, next: final}
  - {id: agent, type: agent, job: implement, output: text, next: waiting}
"""

_V2_DOCUMENT = b"""format_version: 2
start: build
nodes:
  - {id: done, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: build, type: agent, role: builder, job: implement, next: done}
"""

_SINGLE_NODE_DOCUMENT = b"""format_version: 1
start: only
nodes:
  - {id: only, type: subworkflow, operation: add, operands: [2, 3], next: null}
"""

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


def _graph(document: bytes) -> AnyWorkflowGraph:
    graph = parse_workflow_document(document)
    assert isinstance(graph, WorkflowGraph | WorkflowGraphV2)
    return graph


@pytest.mark.parametrize(
    ("document", "sink", "carried"),
    [
        (_V1_DOCUMENT, "final", ("agent", "waiting")),
        (_V2_DOCUMENT, "done", ("build",)),
        (_SINGLE_NODE_DOCUMENT, "only", ()),
    ],
)
def test_the_sink_is_the_node_no_successor_is_configured_for(
    document: bytes, sink: str, carried: tuple[str, ...]
) -> None:
    graph = _graph(document)

    assert graph.sink_node_id == sink
    assert graph.is_sink(sink)
    assert not any(graph.is_sink(node_id) for node_id in carried)


@pytest.mark.parametrize(
    "document", [_V1_DOCUMENT, _V2_DOCUMENT, _SINGLE_NODE_DOCUMENT]
)
def test_exactly_one_node_of_a_document_is_its_sink(document: bytes) -> None:
    graph = _graph(document)

    sinks = [node.id for node in graph.nodes if graph.is_sink(node.id)]

    assert sinks == [graph.sink_node_id]


@pytest.mark.parametrize("document", [_V1_DOCUMENT, _V2_DOCUMENT])
def test_the_sink_is_exactly_the_node_that_has_no_successor_to_advance_to(
    document: bytes,
) -> None:
    graph = _graph(document)

    with pytest.raises(TypeError):
        graph.successor(graph.sink_node_id)

    for node in graph.nodes:
        if node.id == graph.sink_node_id:
            continue
        assert graph.successor(node.id).id == node.next


def test_node_completion_names_the_exact_successor_or_the_end_of_the_run() -> None:
    graph = _graph(_V2_DOCUMENT)

    assert completion_after_node(graph, "build") == RunContinues("done")
    assert completion_after_node(graph, "done") == RunCompletes()


def test_asking_whether_an_unknown_node_is_the_sink_is_refused() -> None:
    graph = _graph(_V2_DOCUMENT)

    with pytest.raises(KeyError):
        graph.is_sink("no-such-node")


def _completed_run(revision_hash: WorkflowRevisionHash, node_id: str) -> Run:
    return Run(
        RunId("run-1"),
        revision_hash,
        RunState.COMPLETED,
        node_id,
        2,
        4,
        Sha256Hash.of(b"terminal"),
    )


def test_a_reentered_completed_run_must_stand_on_the_sink() -> None:
    # Every re-entry after a crash reloads the run through this rule, so it is
    # what decides whether a durable COMPLETED head is coherent at all.
    graph = _graph(_V1_DOCUMENT)
    revision = WorkflowRevision(_V1_DOCUMENT)

    validate_run_graph_binding(
        _completed_run(revision.revision_hash, graph.sink_node_id), graph
    )

    with pytest.raises(RunTransitionConflict):
        validate_run_graph_binding(
            _completed_run(revision.revision_hash, "agent"), graph
        )


def test_a_one_node_document_completes_on_its_only_node() -> None:
    graph = _graph(_SINGLE_NODE_DOCUMENT)
    revision = WorkflowRevision(_SINGLE_NODE_DOCUMENT)

    assert graph.sink_node_id == "only"
    validate_run_graph_binding(_completed_run(revision.revision_hash, "only"), graph)


def test_one_decider_answers_both_graph_families_by_their_own_spelling() -> None:
    # V1 and V2 configure a single successor; V3 declares dependencies. The
    # runtime asks one function, so the family that spells it the other way
    # cannot break a completion run.
    v1 = _graph(_V1_DOCUMENT)
    v3 = v3_graph()

    assert is_sink_node(v1, "final")
    assert not is_sink_node(v1, "agent")

    sinks = v3.sink_node_ids
    assert sinks
    assert all(is_sink_node(v3, node_id) for node_id in sinks)
    assert not any(
        is_sink_node(v3, node.id) for node in v3.nodes if node.id not in sinks
    )


def test_v3_multi_sink_completion_is_refused_until_all_sinks_have_an_owner() -> None:
    graph = parse_workflow_document(_MULTI_SINK_V3_DOCUMENT)

    with pytest.raises(MultipleSinkCompletionUnsupported) as refused:
        is_sink_node(graph, "first")

    assert refused.value.sink_node_ids == ("first", "second")


@pytest.mark.proves("a-completed-v3-attempt-reaches-the-runs-terminal-hash")
def test_a_v3_sink_completes_its_run_and_a_non_sink_is_refused_by_name() -> None:
    """The V3 half this cut needs, and the half it declines to decide.

    A finished V3 node used to reach for a V1 spelling that is not there, which
    raised `AttributeError` from inside a durable transition. The sink half is
    answered here; advancing past a node that is not a sink needs the ready set
    and its join semantics, so it is refused by name instead of guessed.
    """
    graph = v3_graph()
    sink = graph.sink_node_ids[0]
    waiting = next(node.id for node in graph.nodes if node.id != sink)

    assert completion_after_node(graph, sink) == RunCompletes()

    with pytest.raises(ValueError, match="is not a sink"):
        completion_after_node(graph, waiting)
