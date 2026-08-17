"""A run carries its order as material, not as a new published revision.

#38's first sentence is the whole point: a published workflow is reusable with
different orders **without a new revision**. Until now the job travelled inside
the document, so one distinct input burned one revision, and the four sentences
that describe the order as material had no owner able to make them true.

What this pins is the material half: the supplied value is stored immutably
under the run and the name its author declared, the declared context package
names it with the hash of those exact bytes, and the node's request carries it
as a succeeded input envelope. And the refusals: an order the document did not
declare, a declared order nobody supplied, a value the schema refuses and one too
large are each refused **by the name of the input**, before anything is written.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from atelier2.adapters.dbos.runtime import DbosRuntimeSettings, create_canonical_engine
from atelier2.adapters.dbos.schema import (
    initialize_schema,
    node_execution_requests_v3,
    run_inputs_v3,
    runs,
)
from atelier2.adapters.dbos.starter import DbosDurableRunStarter
from atelier2.contracts.node_records_v3 import (
    NodeExecutionRequestHash,
    ProjectedDeliveryStatus,
    RunInput,
)
from atelier2.ports.agent_executions import AgentExecutorRegistry
from atelier2.ports.durable_runs import (
    DurableV3RunCreated,
    DurableV3StartInputRefused,
    V3InputRefusal,
)
from tests.scenarios.v3_ordered_run import (
    MEAL_SCHEMA,
    ORDER_NAME,
    ORDER_VALUE,
    ORDERED_RUN_ID,
    ordered_revision,
    ordered_truth_for,
    publish_order_schemas,
)


@pytest.fixture
def storage(tmp_path: Path) -> Iterator[tuple[Engine, DbosDurableRunStarter]]:
    database = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database)
    initialize_schema(engine)
    publish_order_schemas(engine)
    try:
        yield (
            engine,
            DbosDurableRunStarter(
                engine,
                DbosRuntimeSettings(database, "run-input-test"),
                AgentExecutorRegistry(),
            ),
        )
    finally:
        engine.dispose()


@pytest.mark.proves("a-run-carries-its-order-as-material-not-as-a-new-revision")
def test_the_supplied_order_is_stored_under_the_run_and_the_name_it_was_given(
    storage: tuple[Engine, DbosDurableRunStarter],
) -> None:
    """The bytes the operator handed in, kept where the run can be read back."""
    engine, starter = storage
    decided = ordered_truth_for(ordered_revision())

    assert isinstance(starter.start_v3_with_receipt(decided), DurableV3RunCreated)

    with engine.connect() as connection:
        stored = connection.execute(sa.select(run_inputs_v3)).mappings().one()

    assert str(stored["run_id"]) == ORDERED_RUN_ID.value
    assert str(stored["name"]) == ORDER_NAME
    assert bytes(stored["value"]) == ORDER_VALUE
    assert (
        str(stored["value_hash"])
        == RunInput(
            ORDER_NAME, decided.run_inputs[0].schema_revision, ORDER_VALUE
        ).value_hash.value
    )


@pytest.mark.proves("a-run-carries-its-order-as-material-not-as-a-new-revision")
def test_the_order_reaches_the_node_as_context_and_as_a_bound_input(
    storage: tuple[Engine, DbosDurableRunStarter],
) -> None:
    """Material is only material if the node's own record names it.

    The package the receipt carries names the order with the hash of its exact
    bytes, and the request binds it as a succeeded envelope -- the two places a
    reader asks "what was this node given?" without trusting a projection.
    """
    engine, starter = storage
    decided = ordered_truth_for(ordered_revision())
    supplied = decided.run_inputs[0]

    assert isinstance(starter.start_v3_with_receipt(decided), DurableV3RunCreated)

    envelope = next(
        entry for entry in decided.node_request.inputs if entry.name == ORDER_NAME
    )
    assert envelope.status is ProjectedDeliveryStatus.SUCCEEDED
    assert envelope.schema_revision == supplied.schema_revision
    assert envelope.value_hash == supplied.value_hash
    assert supplied.value_hash.value.encode("ascii") in decided.context_package.manifest
    with engine.connect() as connection:
        stored_request = connection.scalar(
            sa.select(node_execution_requests_v3.c.preimage).where(
                node_execution_requests_v3.c.request_hash
                == decided.node_request.request_hash.value
            )
        )
    assert stored_request is not None
    assert (
        NodeExecutionRequestHash.of(bytes(stored_request))
        == decided.node_request.request_hash
    )


@pytest.mark.proves("a-run-carries-its-order-as-material-not-as-a-new-revision")
def test_the_stored_order_can_never_be_changed_or_removed(
    storage: tuple[Engine, DbosDurableRunStarter],
) -> None:
    """An order that could be edited afterwards is not what the run was given."""
    engine, starter = storage
    assert isinstance(
        starter.start_v3_with_receipt(ordered_truth_for(ordered_revision())),
        DurableV3RunCreated,
    )

    for forbidden in (
        run_inputs_v3.update().values(value=b"another order"),
        run_inputs_v3.delete(),
    ):
        with (
            engine.begin() as connection,
            pytest.raises(IntegrityError, match="run inputs are immutable"),
        ):
            connection.execute(forbidden)


@pytest.mark.proves("an-order-the-start-cannot-honour-is-refused-by-its-own-name")
@pytest.mark.parametrize(
    ("label", "refusal", "detail"),
    (
        ("nothing supplied for a declared order", V3InputRefusal.MISSING, None),
        ("an order the document never declared", V3InputRefusal.UNDECLARED, None),
        (
            "a value the schema refuses",
            V3InputRefusal.VALUE_REFUSED,
            "schema-violated",
        ),
        (
            "a value larger than one order may be",
            V3InputRefusal.VALUE_REFUSED,
            "instance-too-large",
        ),
        (
            "an order pinned to a schema the document did not name",
            V3InputRefusal.SCHEMA_MISMATCH,
            "the document pinned",
        ),
    ),
    ids=("missing", "undeclared", "schema", "size", "other-schema"),
)
def test_an_order_the_start_cannot_honour_is_refused_by_its_own_name(
    storage: tuple[Engine, DbosDurableRunStarter],
    label: str,
    refusal: V3InputRefusal,
    detail: str | None,
) -> None:
    """ADR 0006: a missing graph input refuses the start naming the input.

    Naming the refusal without naming the input would send an operator to read
    the document to find out which order they got wrong.
    """
    engine, starter = storage
    decided = ordered_truth_for(ordered_revision())
    broken = {
        "missing": replace(decided, run_inputs=()),
        "undeclared": replace(
            decided,
            run_inputs=(
                *decided.run_inputs,
                replace(decided.run_inputs[0], name="an-order-nobody-declared"),
            ),
        ),
        "schema-violated": replace(
            decided,
            run_inputs=(replace(decided.run_inputs[0], value=b'{"portions": 0}'),),
        ),
        "instance-too-large": replace(
            decided,
            run_inputs=(
                replace(
                    decided.run_inputs[0], value=b'{"portions": ' + b"1" * 20_000 + b"}"
                ),
            ),
        ),
        # A published, perfectly readable schema -- just not the one the document
        # pinned for this order. Without the revision check the start would
        # validate the value against a schema nobody asked for and admit it.
        "the document pinned": replace(
            decided,
            run_inputs=(
                replace(
                    decided.run_inputs[0], schema_revision=MEAL_SCHEMA.revision_hash
                ),
            ),
        ),
    }[detail or refusal.value]

    answer = starter.start_v3_with_receipt(broken)

    assert isinstance(answer, DurableV3StartInputRefused)
    assert answer.refusal is refusal
    assert answer.name in {ORDER_NAME, "an-order-nobody-declared"}
    if detail is not None:
        assert answer.detail is not None and answer.detail.startswith(detail)
    with engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 0
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(run_inputs_v3))
            == 0
        )
