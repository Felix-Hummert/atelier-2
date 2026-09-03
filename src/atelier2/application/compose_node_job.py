"""The exact text composed for an Agent job or a Wait question.

**Why this exists.** A node presents one authored opening followed by the exact
material it declared. For an Agent those bytes become the execution request; for
a Wait the text becomes the question a person reads. An order or predecessor
result joins that authored opening here so both kinds use one durable composition.

**Why it is its own owner.** The composition decides what a node asks its reader
to do, and a request hash or pause digest is taken over the result, so it is
durable identity rather than formatting. Two callers spelling it slightly
differently would give one node two identities across a retry or restart. It
lives outside the adapter for the same reason it is small: the decision is the
product's, and the adapter's job is only to carry it.
"""

from __future__ import annotations

from enum import StrEnum

from atelier2.contracts.node_records_v3 import DeliveredOutput, RunInput

ORDER_HEADING = "--- order: {name} ---"
"""How one order announces itself, so the reader can tell them apart."""

RESULT_HEADING = "--- result of {node}: {name} ---"
"""How the work of an earlier node announces itself, named by the node that did it."""

OUTPUT_SCHEMA_REPAIR_HEADING = "--- repair: declared output schema ---"


class NodeJobCompositionVersion(StrEnum):
    """The three rendering rules whose bytes have identified agent attempts."""

    LEGACY = "json-orders/v1"
    CURRENT = "declared-root-strings/v2"
    OUTPUT_SCHEMA_REPAIR = "declared-root-strings-with-repair/v3"


class OutputSchemaRepair:
    def __init__(self, refusal_reason: str) -> None:
        if refusal_reason == "":
            raise ValueError("an output-schema repair names a refusal reason")
        self.refusal_reason = refusal_reason


def node_job(
    instruction: str,
    orders: tuple[RunInput, ...] = (),
    results: tuple[DeliveredOutput, ...] = (),
    composition_version: NodeJobCompositionVersion = NodeJobCompositionVersion.CURRENT,
    output_schema_repair: OutputSchemaRepair | None = None,
) -> str:
    """The authored opening, then what this node was given to read.

    Two kinds of material meet here and they stay distinguishable, because they
    answer different questions for the reader: an **order** is what the run was
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
    is_repair_version = (
        composition_version is NodeJobCompositionVersion.OUTPUT_SCHEMA_REPAIR
    )
    if is_repair_version != (output_schema_repair is not None):
        raise ValueError(
            "an output-schema repair requires both its composition version and payload"
        )
    if not orders and not results and output_schema_repair is None:
        return instruction
    sections = [instruction]
    for order in sorted(orders, key=lambda supplied: supplied.name):
        sections.append(ORDER_HEADING.format(name=order.name))
        sections.append(_render_order(order))
    for result in sorted(results, key=lambda done: (done.node_id, done.output_name)):
        sections.append(
            RESULT_HEADING.format(node=result.node_id, name=result.output_name)
        )
        sections.append(result.value.decode("utf-8"))
    if output_schema_repair is not None:
        sections.extend(
            (OUTPUT_SCHEMA_REPAIR_HEADING, output_schema_repair.refusal_reason)
        )
    return "\n\n".join(sections)


def _render_order(order: RunInput) -> str:
    """Render an order exactly as its start admitted it.

    Every stored order's bytes ARE the text a reader should see: a JSON-typed
    order's bytes are its JSON text, and -- since a `"string"`-typed schema
    reads an order as the artifact's own raw UTF-8 text
    (`schemas_v3.read_authored_instance_document`) -- a declared-root-string
    order carries no JSON-quoting layer left to strip here. Both compositions
    therefore render every order identically; `composition_version` stays
    `node_job`'s own parameter, for the repair heading it still gates.
    """
    return order.value.decode("utf-8")
