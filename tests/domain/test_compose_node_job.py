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
from atelier2.contracts.node_records_v3 import RunInput, RunInputSchemaKind
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


def test_a_declared_root_string_order_renders_its_raw_text_verbatim() -> None:
    """Since #1091, a `\"string\"`-typed order's stored bytes ARE its text.

    `read_authored_instance_document` admits the order's raw UTF-8 text
    directly, so there is no JSON-quoting layer left here to strip -- unlike a
    JSON-typed order, whose stored bytes stay its literal JSON text.
    """
    composed = node_job(
        "Review it.",
        (
            RunInput(
                "diff",
                SCHEMA,
                b"diff --git a/file.py b/file.py\n+line",
                RunInputSchemaKind.PLAIN_STRING,
            ),
            RunInput("metadata", SCHEMA, b'{ "files": 1 }'),
            RunInput("summary", SCHEMA, b'"ship it"', RunInputSchemaKind.JSON),
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


def test_the_legacy_composition_also_renders_a_declared_root_strings_raw_text() -> None:
    composed = node_job(
        "Review it.",
        (
            RunInput(
                "diff",
                SCHEMA,
                b"diff --git a/file.py b/file.py\n+line",
                RunInputSchemaKind.PLAIN_STRING,
            ),
        ),
        composition_version=NodeJobCompositionVersion.LEGACY,
    )

    assert composed == "\n\n".join(
        [
            "Review it.",
            ORDER_HEADING.format(name="diff"),
            "diff --git a/file.py b/file.py\n+line",
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
        pytest.param(
            NodeJobCompositionVersion.LEGACY,
            OutputSchemaRepair("output-schema-refused: instance-not-json"),
            id="legacy-version-with-repair-payload",
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


def test_current_and_legacy_compositions_agree_on_a_declared_root_strings_preimage() -> (
    None
):
    """Since #1091, the two versions render an order identically.

    They used to diverge because the current composition stripped a
    JSON-quoting layer the legacy one kept; that layer no longer exists (a
    `\"string\"`-typed order's stored bytes are already its raw text), so the
    request hash a caller gets is the same under either name.
    """
    supplied = (
        RunInput(
            "diff",
            SCHEMA,
            b"diff --git a/file.py b/file.py\n+line",
            RunInputSchemaKind.PLAIN_STRING,
        ),
    )
    expected_preimage = (
        b"Review it.\n\n--- order: diff ---\n\ndiff --git a/file.py b/file.py\n+line"
    )

    for composition_version in (
        NodeJobCompositionVersion.LEGACY,
        NodeJobCompositionVersion.CURRENT,
    ):
        assert (
            node_job(
                "Review it.", supplied, composition_version=composition_version
            ).encode("utf-8")
            == expected_preimage
        )
