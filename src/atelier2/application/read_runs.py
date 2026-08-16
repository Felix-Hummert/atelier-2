"""Reading runs, as decisions rather than port answers.

`get_run` has one caller shape the others do not: several command routes read a
run back to answer with its current resource after they changed it. That is the
same decision, so it is this one use-case rather than a second path, and the
caller above keeps only the rendering.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.refusals import DurableStateCorrupt, ReadUnavailable
from atelier2.contracts.runs import RunId
from atelier2.ports.run_queries import (
    RunFound,
    RunPage,
    RunProjection,
    RunQueries,
    RunQueryMissing,
)
from atelier2.ports.workflow_revisions import (
    DurableProjectionLimit,
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


type GetRunResult = RunRead | RunNotFound | ReadUnavailable | DurableStateCorrupt
type ListRunsResult = RunsListed | ReadUnavailable | DurableStateCorrupt


def get_run(
    run_id: RunId,
    projection_limit: DurableProjectionLimit,
    queries: RunQueries,
) -> GetRunResult:
    match queries.get_run(run_id, projection_limit):
        case RunFound(projection):
            return RunRead(projection)
        case RunQueryMissing():
            return RunNotFound()
        case PortReadUnavailable(detail):
            return ReadUnavailable(detail)
        case QueryDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)


def list_runs(
    after: RunId | None,
    limit: int,
    projection_limit: DurableProjectionLimit,
    queries: RunQueries,
) -> ListRunsResult:
    match queries.list_runs(after, limit, projection_limit):
        case RunPage(runs, next_after):
            return RunsListed(runs, next_after)
        case PortReadUnavailable(detail):
            return ReadUnavailable(detail)
        case QueryDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
