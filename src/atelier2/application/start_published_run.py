from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.publish_workflow_revision import (
    DurableStateCorrupt,
    WriteUnavailable,
)
from atelier2.contracts.runs import Run
from atelier2.ports.durable_runs import (
    DurablePublishedRunStarter,
    DurableRunCreated,
    DurableRunExisting,
    DurableRunIdentityConflict,
    DurableRunRevisionMissing,
    DurableWriteUnavailable,
    StartPublishedRunRequest,
)
from atelier2.ports.durable_runs import (
    DurableStateCorrupt as PortDurableStateCorrupt,
)


@dataclass(frozen=True)
class RunCreated:
    run: Run


@dataclass(frozen=True)
class RunExisting:
    run: Run


@dataclass(frozen=True)
class RevisionMissing:
    pass


@dataclass(frozen=True)
class RunIdentityConflict:
    pass


type StartPublishedRunResult = (
    RunCreated
    | RunExisting
    | RevisionMissing
    | RunIdentityConflict
    | WriteUnavailable
    | DurableStateCorrupt
)


def start_published_run(
    request: StartPublishedRunRequest, starter: DurablePublishedRunStarter
) -> StartPublishedRunResult:
    result = starter.start_published(request)
    match result:
        case DurableRunCreated(run):
            return RunCreated(run)
        case DurableRunExisting(run):
            return RunExisting(run)
        case DurableRunRevisionMissing():
            return RevisionMissing()
        case DurableRunIdentityConflict():
            return RunIdentityConflict()
        case DurableWriteUnavailable():
            return WriteUnavailable()
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
