"""Reading runs, as decisions rather than port answers.

`get_run` has one caller shape the others do not: several command routes read a
run back to answer with its current resource after they changed it. That is the
same decision, so it is this one use-case rather than a second path, and the
caller above keeps only the rendering.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.refusals import (
    DurableStateCorrupt,
    ProjectionTooLarge,
    ReadUnavailable,
)
from atelier2.contracts.run_projections import (
    RunPage,
    RunProjection,
)
from atelier2.contracts.runs import RunId
from atelier2.ports.run_queries import (
    RunFound,
    RunQueries,
    RunQueryMissing,
)
from atelier2.ports.workflow_revisions import (
    ProjectionTooLarge as PortProjectionTooLarge,
)
from atelier2.ports.workflow_revisions import (
    QueryDurableStateCorrupt,
)
from atelier2.ports.workflow_revisions import (
    ReadUnavailable as PortReadUnavailable,
)


@dataclass(frozen=True)
class RunRead:
    projection: RunProjection


@dataclass(frozen=True)
class RunNotFound:
    pass


@dataclass(frozen=True)
class RunsListed:
    runs: tuple[RunProjection, ...]
    next_after: RunId | None


type GetRunResult = (
    RunRead | RunNotFound | ReadUnavailable | ProjectionTooLarge | DurableStateCorrupt
)
type ListRunsResult = (
    RunsListed | ReadUnavailable | ProjectionTooLarge | DurableStateCorrupt
)


def get_run(
    run_id: RunId,
    queries: RunQueries,
) -> GetRunResult:
    match queries.get_run(run_id):
        case RunFound(projection):
            return RunRead(projection)
        case RunQueryMissing():
            return RunNotFound()
        case PortReadUnavailable(detail):
            return ReadUnavailable(detail)
        case PortProjectionTooLarge():
            return ProjectionTooLarge()
        case QueryDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)


def list_runs(
    after: RunId | None,
    limit: int,
    queries: RunQueries,
) -> ListRunsResult:
    match queries.list_runs(after, limit):
        case RunPage(runs, next_after):
            return RunsListed(runs, next_after)
        case PortReadUnavailable(detail):
            return ReadUnavailable(detail)
        case PortProjectionTooLarge():
            return ProjectionTooLarge()
        case QueryDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
