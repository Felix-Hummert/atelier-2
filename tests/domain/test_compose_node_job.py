"""What an agent is handed for a node that reads orders.

The composition is durable identity rather than formatting: the node execution's
request hash is taken over the result, so two spellings of the same job would
give one node two identities across a retry. That is why this is pinned at its
own owner and not only through the callers that happen to hand it sorted input
today -- a promise that holds by accident of its callers is not a promise.
"""

from __future__ import annotations

import pytest

from atelier2.application.compose_node_job import (
    ORDER_HEADING,
    OUTPUT_SCHEMA_REPAIR_HEADING,
    NodeJobCompositionVersion,
    OutputSchemaRepair,
    node_job,
)
from atelier2.contracts.node_records_v3 import RunInput
from atelier2.contracts.revisions_v3 import PublishedRevisionHash

SCHEMA = PublishedRevisionHash("a" * 64)


def order(name: str, value: bytes) -> RunInput:
    return RunInput(name, SCHEMA, value)


def test_a_node_that_reads_no_order_is_asked_exactly_what_its_author_wrote() -> None:
    assert node_job("Cook it.", ()) == "Cook it."


def test_the_orders_follow_the_instruction_each_announced_by_its_name() -> None:
    composed = node_job("Cook it.", (order("portions", b"4"),))

    assert composed == "\n\n".join(
        ["Cook it.", ORDER_HEADING.format(name="portions"), "4"]
    )


def test_the_same_orders_compose_the_same_job_whatever_order_they_arrive_in() -> None:
    """Arrival order is the caller's; the job is the run's.

    A start that supplies the same orders in a different sequence is the same
    run -- `run_inputs_v3` keeps no position on purpose -- so a job that followed
    arrival would make a retry of that run a different execution.
    """
    supplied = (order("side", b"9"), order("main", b"4"))

    assert node_job("Plate it.", supplied) == node_job("Plate it.", supplied[::-1])
    assert node_job("Plate it.", supplied).index(
        ORDER_HEADING.format(name="main")
    ) < node_job("Plate it.", supplied).index(ORDER_HEADING.format(name="side"))


def test_an_orders_stored_bytes_render_verbatim_whatever_schema_admitted_them() -> None:
    """Since #1091, every order's stored bytes ARE the text a reader should see.

    A `"string"`-typed order's stored bytes are already its raw UTF-8 text
    (`schemas_v3.read_authored_instance_document` is the one door that reads
    it that way at the start); every other schema keeps its own JSON text.
    Either way, `RunInput` carries no rendering-relevant schema kind any more
    (#1091 finding 3): the composition has nothing left to strip.
    """
    composed = node_job(
        "Review it.",
        (
            order("diff", b"diff --git a/file.py b/file.py\n+line"),
            order("metadata", b'{ "files": 1 }'),
            order("summary", b'"ship it"'),
        ),
    )

    assert composed == "\n\n".join(
        [
            "Review it.",
            ORDER_HEADING.format(name="diff"),
            "diff --git a/file.py b/file.py\n+line",
            ORDER_HEADING.format(name="metadata"),
            '{ "files": 1 }',
            ORDER_HEADING.format(name="summary"),
            '"ship it"',
        ]
    )


def test_output_schema_repair_is_a_versioned_composition_input() -> None:
    composed = node_job(
        "Review it.",
        output_schema_repair=OutputSchemaRepair(
            "output-schema-refused: instance-not-json"
        ),
        composition_version=NodeJobCompositionVersion.OUTPUT_SCHEMA_REPAIR,
    )

    assert composed == (
        "Review it.\n\n"
        f"{OUTPUT_SCHEMA_REPAIR_HEADING}\n\n"
        "output-schema-refused: instance-not-json"
    )


@pytest.mark.parametrize(
    ("composition_version", "repair"),
    [
        pytest.param(
            NodeJobCompositionVersion.OUTPUT_SCHEMA_REPAIR,
            None,
            id="repair-version-without-receipt-payload",
        ),
        pytest.param(
            NodeJobCompositionVersion.CURRENT,
            OutputSchemaRepair("output-schema-refused: instance-not-json"),
            id="current-version-with-repair-payload",
        ),
    ],
)
def test_node_job_refuses_every_version_and_repair_payload_mismatch(
    composition_version: NodeJobCompositionVersion,
    repair: OutputSchemaRepair | None,
) -> None:
    with pytest.raises(ValueError, match="output-schema repair"):
        node_job(
            "Review it.",
            composition_version=composition_version,
            output_schema_repair=repair,
        )
