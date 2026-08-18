"""The store surface that ends a run nothing can continue."""

from __future__ import annotations

from typing import Protocol

from atelier2.contracts.runs import RunId


class UncontinuableRunStore(Protocol):
    def uncontinuable_runs(self) -> tuple[RunId, ...]:
        """Every STARTED run whose current node has terminally failed.

        A non-terminal attempt on that run is a replacement still in flight,
        so those rows stay out.
        """
        ...

    def end_uncontinuable_run(self, run_id: RunId) -> bool:
        """End that run as FAILED under the events it already has.

        Returns False when another writer ended it first. Does not write a
        new event: the AGENT_FAILED that made the line uncontinuable is
        already there; this only lifts the snapshot to match.
        """
        ...
