"""`RunResourceV3`'s ask-once row fields, told safely (#1045).

`run_resource` is a pure function of `RunProjection`, exactly as
`test_run_orders_projection.py` already proves for `orders` (and now for the
order-derived `work_item_reference` beside them) -- so `workflow_name` and
the terminal `answer` / `refusal_output` pin here without a database too.
History used to ask `getWorkflowRevision` once per distinct revision hash and
`getNodeDetail` once per row for exactly these facts (REQ-UIQ-08); this is
that same data carried on the row a list already returns.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from atelier2.api.projection.runs import run_resource
from atelier2.api.references import MAXIMUM_RUN_TERMINAL_ANSWER_BYTES
from atelier2.api.wire.resources import RunResourceV3
from atelier2.contracts.agents import AgentBindingSet
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.node_records_v3 import RunInput
from atelier2.contracts.run_bindings import RunV3
from atelier2.contracts.run_configuration_v3 import RunConfigurationRevisionHash
from atelier2.contracts.run_projections import NodeAnswer, RunProjection
from atelier2.contracts.runs import RunId, RunState, WorkflowRevisionHash
from atelier2.contracts.workflows_v3 import AgentNodeV3, WorkflowGraphV3

RUN_ID = RunId("run-with-terminal-result")
REVISION_HASH = WorkflowRevisionHash("a" * 64)
NODE_ID = "final"
TERMINAL_HASH = Sha256Hash.of(b"terminal")


def _graph(name: str = "Two agents in a line") -> WorkflowGraphV3:
    return WorkflowGraphV3(
        format_version=3,
        name=name,
        nodes=(
            AgentNodeV3(
                id=NODE_ID,
                type="agent",
                role="builder",
                mode="headless",
                instruction="Do the one thing.",
            ),
        ),
    )


def _run(
    state: RunState = RunState.STARTED,
    terminal_hash: Sha256Hash | None = None,
) -> RunV3:
    return RunV3(
        RUN_ID,
        REVISION_HASH,
        AgentBindingSet(()).binding_set_hash,
        (),
        state,
        NODE_ID,
        1,
        1,
        RunConfigurationRevisionHash("c" * 64),
        terminal_hash,
    )


def _projection(
    *,
    state: RunState = RunState.STARTED,
    terminal_hash: Sha256Hash | None = None,
    name: str = "Two agents in a line",
    orders: tuple[RunInput, ...] = (),
    answer: NodeAnswer | None = None,
    refusal_output: NodeAnswer | None = None,
) -> RunProjection:
    return RunProjection(
        _run(state, terminal_hash),
        _graph(name),
        None,
        (),
        orders=orders,
        answer=answer,
        refusal_output=refusal_output,
    )


def test_a_run_names_the_workflow_document_declared() -> None:
    resource = run_resource(_projection(name="code-review"))

    assert isinstance(resource, RunResourceV3)
    assert resource.workflow_name == "code-review"


def test_a_completed_run_carries_its_terminal_answer_never_a_refusal() -> None:
    answer = NodeAnswer(
        b'{"answer":"PR merged"}', Sha256Hash.of(b'{"answer":"PR merged"}')
    )

    resource = run_resource(
        _projection(
            state=RunState.COMPLETED, terminal_hash=TERMINAL_HASH, answer=answer
        )
    )

    assert isinstance(resource, RunResourceV3)
    assert resource.answer is not None
    assert resource.answer.value_base64 == "eyJhbnN3ZXIiOiJQUiBtZXJnZWQifQ=="
    assert resource.answer.value_hash == answer.value_hash.value
    assert resource.refusal_output is None


def test_a_failed_run_carries_its_refusal_output_never_an_answer() -> None:
    refusal = NodeAnswer(b"schema refused this", Sha256Hash.of(b"schema refused this"))

    resource = run_resource(
        _projection(
            state=RunState.FAILED, terminal_hash=TERMINAL_HASH, refusal_output=refusal
        )
    )

    assert isinstance(resource, RunResourceV3)
    assert resource.refusal_output is not None
    assert resource.refusal_output.value_hash == refusal.value_hash.value
    assert resource.answer is None


def test_a_live_run_carries_neither_a_terminal_answer_nor_a_refusal() -> None:
    resource = run_resource(_projection(state=RunState.STARTED))

    assert isinstance(resource, RunResourceV3)
    assert resource.answer is None
    assert resource.refusal_output is None


def test_a_terminal_answer_over_the_list_bound_is_nulled_not_truncated() -> None:
    oversized = b"a" * (MAXIMUM_RUN_TERMINAL_ANSWER_BYTES + 1)
    answer = NodeAnswer(oversized, Sha256Hash.of(oversized))

    resource = run_resource(
        _projection(
            state=RunState.COMPLETED, terminal_hash=TERMINAL_HASH, answer=answer
        )
    )

    assert isinstance(resource, RunResourceV3)
    assert resource.answer is None


def test_a_terminal_answer_at_the_list_bound_is_admitted() -> None:
    fits = b"a" * MAXIMUM_RUN_TERMINAL_ANSWER_BYTES
    answer = NodeAnswer(fits, Sha256Hash.of(fits))

    resource = run_resource(
        _projection(
            state=RunState.COMPLETED, terminal_hash=TERMINAL_HASH, answer=answer
        )
    )

    assert isinstance(resource, RunResourceV3)
    assert resource.answer is not None
    assert resource.answer.value_hash == answer.value_hash.value


def test_a_live_run_refuses_a_terminal_answer_the_wire_never_admits() -> None:
    answer = NodeAnswer(b"too early", Sha256Hash.of(b"too early"))

    with pytest.raises(ValidationError):
        run_resource(_projection(state=RunState.STARTED, answer=answer))


def test_a_run_never_names_both_a_terminal_answer_and_a_refusal() -> None:
    answer = NodeAnswer(b"ok", Sha256Hash.of(b"ok"))
    refusal = NodeAnswer(b"refused", Sha256Hash.of(b"refused"))

    with pytest.raises(ValidationError):
        run_resource(
            _projection(
                state=RunState.FAILED,
                terminal_hash=TERMINAL_HASH,
                answer=answer,
                refusal_output=refusal,
            )
        )
