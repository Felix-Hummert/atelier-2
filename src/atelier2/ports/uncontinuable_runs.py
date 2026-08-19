"""The store surface that ends a run nothing can continue."""

from __future__ import annotations

from typing import Protocol

from atelier2.contracts.runs import RunId


class UncontinuableRunStore(Protocol):
    def uncontinuable_runs(self) -> tuple[RunId, ...]:
        """Every STARTED run whose current node can no longer continue.

        Either the current node's last attempt is already FAILED or
        INTERRUPTED, or the run advanced past a succeeded predecessor onto a
        node that has no attempt and whose durable node workflow is not
        recoverable. No non-terminal attempt is still in flight. A V1 run
        stays out: the frozen V1 wire cannot end as FAILED.
        """
        ...

    def end_uncontinuable_run(self, run_id: RunId) -> bool:
        """End that run as FAILED under the events it already has.

        Returns False when another writer ended it first. Does not write a
        new event: the AGENT_FAILED or AGENT_INTERRUPTED that made the
        attempt family uncontinuable is already there, and the gap family's
        named reason is the failed node-receipt/v3 under
        atelier2-driver-lost. This only lifts the snapshot to match.
        """
        ...
