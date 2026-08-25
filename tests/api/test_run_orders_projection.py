"""A V3 run's orders on `RunResourceV3`, straight from the store's own record.

`_run_resource_v3` is a pure function of `RunProjection`, so both shapes pin
here without a database: a run started with an order shows it, base64 exactly
as `RunInput` held it; a run started with none answers an empty list, its own
honest answer rather than a gap this projection fills in.
"""

from __future__ import annotations

from atelier2.api.projection.runs import run_resource
from atelier2.api.references import encode_canonical_base64
from atelier2.api.wire.resources import RunOrderResource, RunResourceV3
from atelier2.contracts.agents import AgentBindingSet
from atelier2.contracts.node_records_v3 import RunInput
from atelier2.contracts.revisions_v3 import PublishedRevisionHash
from atelier2.contracts.run_bindings import RunV3
from atelier2.contracts.run_configuration_v3 import RunConfigurationRevisionHash
from atelier2.contracts.run_projections import RunProjection
from atelier2.contracts.runs import RunId, RunState, WorkflowRevisionHash
from atelier2.contracts.workflows_v3 import AgentNodeV3, WorkflowGraphV3

RUN_ID = RunId("run-with-orders")
REVISION_HASH = WorkflowRevisionHash("a" * 64)
NODE_ID = "cook"


def _graph() -> WorkflowGraphV3:
    return WorkflowGraphV3(
        format_version=3,
        name="One agent, whichever orders it was started with",
        nodes=(
            AgentNodeV3(
                id=NODE_ID,
                type="agent",
                role="cook",
                mode="headless",
                instruction="Cook exactly what the order says.",
            ),
        ),
    )


def _projection(orders: tuple[RunInput, ...]) -> RunProjection:
    return RunProjection(
        RunV3(
            RUN_ID,
            REVISION_HASH,
            AgentBindingSet(()).binding_set_hash,
            (),
            RunState.STARTED,
            NODE_ID,
            0,
            0,
            RunConfigurationRevisionHash("c" * 64),
        ),
        _graph(),
        None,
        (),
        orders=orders,
    )


def test_a_run_with_an_order_shows_it_on_the_resource() -> None:
    order = RunInput("headline", PublishedRevisionHash("b" * 64), b'{"topic":"launch"}')

    resource = run_resource(_projection((order,)))

    assert isinstance(resource, RunResourceV3)
    assert resource.orders == (
        RunOrderResource(
            name="headline",
            value_base64=encode_canonical_base64(order.value),
        ),
    )


def test_a_run_without_orders_shows_an_empty_list() -> None:
    resource = run_resource(_projection(()))

    assert isinstance(resource, RunResourceV3)
    assert resource.orders == ()
