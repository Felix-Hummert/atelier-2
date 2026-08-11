from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, assert_never

from atelier2.application.publish_workflow_revision import (
    DurableStateCorrupt,
    WriteUnavailable,
)
from atelier2.contracts.runs import Run, RunId, WorkflowRevisionHash
from atelier2.ports.durable_runs import (
    DurablePublishedRunResult,
    DurableRunCreated,
    DurableRunExisting,
    DurableRunIdentityConflict,
    DurableRunRevisionMissing,
    DurableWriteUnavailable,
)
from atelier2.ports.durable_runs import (
    DurableStateCorrupt as PortDurableStateCorrupt,
)


@dataclass(frozen=True)
class StartPublishedRunRequest:
    run_id: RunId
    revision_hash: WorkflowRevisionHash


class DurablePublishedRunStarter(Protocol):
    def start_published(
        self, request: StartPublishedRunRequest
    ) -> DurablePublishedRunResult: ...


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
