"""End every STARTED run whose open node path has already failed.

#302 stops attempts whose driver is gone. This is the half that item left
ownerless: the attempt is already FAILED, the run still says STARTED, and
nothing will ever move it. A serve start walks those rows and lifts the
node's ending onto the run — the same reason, one level up.
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
