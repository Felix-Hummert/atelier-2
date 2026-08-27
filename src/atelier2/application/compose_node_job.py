"""What an agent is actually handed for a node that reads an order.

**Why this exists.** An agent receives exactly one thing: the job bytes of its
execution request. Until now those bytes were the authored instruction and
nothing else, which is why a distinct input meant a distinct published revision --
the only way to tell an agent something new was to write it into the document.
An order is the material that ends that, so this is where the order joins the
instruction.

**Why it is its own owner.** The composition decides what an agent is asked to
do, and the request hash is taken over the result, so it is durable identity
rather than formatting. Two callers spelling it slightly differently would give
one node two identities across a retry. It lives outside the adapter for the same
reason it is small: the decision is the product's, and the adapter's job is only
to carry it.
"""

from __future__ import annotations

import json
from enum import StrEnum

from atelier2.contracts.node_records_v3 import (
    DeliveredOutput,
    RunInput,
    RunInputSchemaKind,
)

ORDER_HEADING = "--- order: {name} ---"
"""How one order announces itself inside the job, so the agent can tell them apart."""

RESULT_HEADING = "--- result of {node}: {name} ---"
"""How the work of an earlier node announces itself, named by the node that did it."""


class NodeJobCompositionVersion(StrEnum):
    """The two rendering rules whose bytes have identified agent attempts."""

    LEGACY = "json-orders/v1"
    CURRENT = "declared-root-strings/v2"


def node_job(
    instruction: str,
    orders: tuple[RunInput, ...] = (),
    results: tuple[DeliveredOutput, ...] = (),
    composition_version: NodeJobCompositionVersion = NodeJobCompositionVersion.CURRENT,
) -> str:
    """The instruction its author wrote, then what this node was given to read.

    Two kinds of material meet here and they stay distinguishable, because they
    answer different questions for the agent: an **order** is what the run was
    started with, and a **result** is what an earlier node produced. Each
    announces itself under the name its author declared, and a result also names
    the node that did the work, so a reader can tell whose sentence it is looking
    at without the composition inventing prose.

    Both are ordered by name rather than by arrival, and results after orders, so
    the same run asks the same thing whatever sequence the material reached this
    layer in -- a request hash that depended on that would make a retry a
    different execution.

    A value is decoded as UTF-8 because it was already read as a document against
    the schema its author pinned -- an order at the start, a result before it is
    handed on -- so bytes that were not text could not have reached here.
    """
    if not isinstance(composition_version, NodeJobCompositionVersion):
        raise TypeError("node job composition version must be typed")
    if not orders and not results:
        return instruction
    sections = [instruction]
    for order in sorted(orders, key=lambda supplied: supplied.name):
        sections.append(ORDER_HEADING.format(name=order.name))
        sections.append(_render_order(order, composition_version))
    for result in sorted(results, key=lambda done: (done.node_id, done.output_name)):
        sections.append(
            RESULT_HEADING.format(node=result.node_id, name=result.output_name)
        )
        sections.append(result.value.decode("utf-8"))
    return "\n\n".join(sections)


def _render_order(
    order: RunInput, composition_version: NodeJobCompositionVersion
) -> str:
    """Render an order under the version that authored the request hash.

    The legacy composition wrote every order's stored JSON bytes. The current
    composition decodes only declared root strings. Both preserve the stored
    bytes and their hash; only the text an agent reads differs.
    """
    value = order.value.decode("utf-8")
    if (
        composition_version is NodeJobCompositionVersion.LEGACY
        or order.schema_kind is not RunInputSchemaKind.PLAIN_STRING
    ):
        return value
    decoded = json.loads(value)
    if not isinstance(decoded, str):
        raise TypeError("a plain-string schema admitted a non-string order")
    return decoded
