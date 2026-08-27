"""What an agent is handed for a node that reads orders.

The composition is durable identity rather than formatting: the node execution's
request hash is taken over the result, so two spellings of the same job would
give one node two identities across a retry. That is why this is pinned at its
own owner and not only through the callers that happen to hand it sorted input
today -- a promise that holds by accident of its callers is not a promise.
"""

from __future__ import annotations

from atelier2.application.compose_node_job import ORDER_HEADING, node_job
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


def test_only_a_declared_root_string_order_is_rendered_raw() -> None:
    composed = node_job(
        "Review it.",
        (
            RunInput(
                "diff",
                SCHEMA,
                b'"diff --git a/file.py b/file.py\\n+line"',
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
