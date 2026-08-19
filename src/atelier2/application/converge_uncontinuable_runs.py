"""End every STARTED run whose open node path can no longer continue.

#302 stops attempts whose driver is gone and leaves them INTERRUPTED.
#339 lifts a leftover FAILED attempt onto the run. This is the remaining
half of that inventory: the attempt is already INTERRUPTED under
`atelier2-driver-lost`, or the run advanced onto a node that never
prepared and whose durable workflow will not recover, the run still says
STARTED, and nothing will ever move it. A serve start walks those rows
and lifts the existing ending onto the run — the same reason, one level
up. A gap node that never received a durable request stays honestly
receipt-less; the lift does not invent one. A V1 row stays out.
"""

from __future__ import annotations

from atelier2.contracts.runs import RunId
from atelier2.ports.uncontinuable_runs import UncontinuableRunStore


def converge_uncontinuable_runs(store: UncontinuableRunStore) -> tuple[RunId, ...]:
    """End each uncontinuable run, and answer with the ones that ended here."""

    ended: list[RunId] = []
    for run_id in store.uncontinuable_runs():
        if store.end_uncontinuable_run(run_id):
            ended.append(run_id)
    return tuple(ended)
