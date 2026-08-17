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

from atelier2.contracts.node_records_v3 import RunInput

ORDER_HEADING = "--- order: {name} ---"
"""How one order announces itself inside the job, so the agent can tell them apart."""


def node_job(instruction: str, orders: tuple[RunInput, ...]) -> str:
    """The instruction its author wrote, followed by the orders this node reads.

    Orders are ordered by name rather than by arrival, so the same run started
    with the same orders is asked the same thing whatever order they were
    supplied in -- a request hash that depended on that would make a retry a
    different execution.

    A value is decoded as UTF-8 because the start already read it as a JSON
    document against the schema its author pinned; bytes that were not text could
    not have reached a stored order.
    """
    if not orders:
        return instruction
    sections = [instruction]
    for order in sorted(orders, key=lambda supplied: supplied.name):
        sections.append(ORDER_HEADING.format(name=order.name))
        sections.append(order.value.decode("utf-8"))
    return "\n\n".join(sections)
