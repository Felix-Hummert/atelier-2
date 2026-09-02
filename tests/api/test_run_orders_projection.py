"""A V3 run's orders on `RunResourceV3`, told safely -- never the order's own bytes.

`_run_resource_v3` is a pure function of `RunProjection`, so every shape pins
here without a database: a run started with an order shows its name, size and
its pinned schema, never the bytes themselves; a run started with none
answers an empty list, its own honest answer rather than a gap this
projection fills in. The admission path (`ApiLimits.require_run_projection`)
is the edge that refuses a projection carrying more orders than the wire
allows, before the Pydantic model ever sees it.

No text preview of an order's material travels here: that needs the
redaction owner #666 is building, and #738's own body names it as the next
step once that owner lands.

`work_item_reference` (#1045) is read from these same orders, against the
one published `work_item` order schema, never a second guess parsed from a
node's job -- so its shape pins here too, beside the orders it is derived
from.
"""

from __future__ import annotations

import pytest

from atelier2.api.limits import ApiLimitExceeded
from atelier2.api.projection.runs import run_resource
from atelier2.api.references import MAXIMUM_RUN_ORDERS
from atelier2.api.wire.resources import RunOrderResource, RunResourceV3
from atelier2.contracts.agents import AgentBindingSet
from atelier2.contracts.node_records_v3 import RunInput
from atelier2.contracts.queue_projection import TrackerItemReference
from atelier2.contracts.revisions_v3 import PublishedRevisionHash
from atelier2.contracts.run_bindings import RunV3
from atelier2.contracts.run_configuration_v3 import RunConfigurationRevisionHash
from atelier2.contracts.run_projections import RunProjection
from atelier2.contracts.runs import RunId, RunState, WorkflowRevisionHash
from atelier2.contracts.when import RecordedAt
from atelier2.contracts.work_items import (
    WORK_ITEM_ORDER_SCHEMA_REVISION,
    ObservedWorkItemRevision,
    WorkItemChangeMarker,
    WorkItemKind,
    work_item_order_document,
)
from atelier2.contracts.workflows_v3 import AgentNodeV3, WorkflowGraphV3
from tests.scenarios.api import api_limits

RUN_ID = RunId("run-with-orders")
REVISION_HASH = WorkflowRevisionHash("a" * 64)
NODE_ID = "cook"
SCHEMA_HASH = PublishedRevisionHash("b" * 64)


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


def test_a_run_with_an_order_shows_its_shape_and_never_its_bytes() -> None:
    order = RunInput("headline", SCHEMA_HASH, b'{"topic":"launch"}')

    resource = run_resource(_projection((order,)))

    assert isinstance(resource, RunResourceV3)
    assert resource.orders == (
        RunOrderResource(
            name="headline",
            bytes=len(order.value),
            schema_revision_hash=SCHEMA_HASH.value,
        ),
    )


def test_a_run_without_orders_shows_an_empty_list() -> None:
    resource = run_resource(_projection(()))

    assert isinstance(resource, RunResourceV3)
    assert resource.orders == ()


def test_a_projection_carrying_more_orders_than_the_wire_allows_is_refused() -> None:
    orders = tuple(
        RunInput(f"order-{index}", SCHEMA_HASH, b"v")
        for index in range(MAXIMUM_RUN_ORDERS + 1)
    )

    with pytest.raises(ApiLimitExceeded):
        api_limits().require_run_projection(_projection(orders))


def test_a_projection_at_the_order_bound_is_admitted() -> None:
    orders = tuple(
        RunInput(f"order-{index}", SCHEMA_HASH, b"v")
        for index in range(MAXIMUM_RUN_ORDERS)
    )

    api_limits().require_run_projection(_projection(orders))


def _work_item_order(reference: str) -> RunInput:
    revision = ObservedWorkItemRevision(
        item=TrackerItemReference(reference),
        kind=WorkItemKind.ISSUE,
        body=b"an issue body",
        change_marker=WorkItemChangeMarker("etag-1"),
        observed_at=RecordedAt("2026-08-26T09:15:00Z"),
    )
    return RunInput(
        "work_item", WORK_ITEM_ORDER_SCHEMA_REVISION, work_item_order_document(revision)
    )


def test_a_run_with_a_work_item_order_names_its_reference() -> None:
    resource = run_resource(_projection((_work_item_order("gh:567"),)))

    assert isinstance(resource, RunResourceV3)
    assert resource.work_item_reference == "gh:567"


def test_a_run_without_a_work_item_order_names_none() -> None:
    resource = run_resource(_projection(()))

    assert isinstance(resource, RunResourceV3)
    assert resource.work_item_reference is None


def test_an_order_pinned_to_another_schema_is_never_a_work_item_reference() -> None:
    order = RunInput("headline", SCHEMA_HASH, b'{"reference":"gh:1"}')

    resource = run_resource(_projection((order,)))

    assert isinstance(resource, RunResourceV3)
    assert resource.work_item_reference is None
