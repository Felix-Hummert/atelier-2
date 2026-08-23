"""Reading and publishing the host channel's project-root revisions.

The channel's own vocabulary, not a second one: a malformed project id is
`project-unknown`, exactly as `ProjectId` itself raises it; a project with no
configured root is the channel's own `project-root-missing`, distinct from the
`project-unknown` compose and the runtime answer when they translate that same
miss at bootstrap (`docs/decisions/0001-durable-runtime.md`, V25 changelog); an
unreadable or unwritable channel is `host-configuration-unreadable`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import assert_never

from atelier2.application.refusals import DurableStateCorrupt
from atelier2.contracts.host_configuration import (
    ProjectId,
    ProjectRootRevision,
    ProjectUnknown,
)
from atelier2.ports.durable_runs import DurableStateCorrupt as PortDurableStateCorrupt
from atelier2.ports.host_configuration import HostConfigurationChannel
from atelier2.ports.host_configuration import (
    HostConfigurationReadUnavailable as PortHostConfigurationReadUnavailable,
)
from atelier2.ports.host_configuration import (
    ProjectRootRevisionConflict as PortProjectRootRevisionConflict,
)
from atelier2.ports.host_configuration import (
    ProjectRootRevisionCreated as PortProjectRootRevisionCreated,
)
from atelier2.ports.host_configuration import (
    ProjectRootRevisionExisting as PortProjectRootRevisionExisting,
)


@dataclass(frozen=True)
class ProjectRootRead:
    revision: ProjectRootRevision


@dataclass(frozen=True)
class ProjectRootMissing:
    pass


@dataclass(frozen=True)
class ProjectRootProjectUnknown:
    pass


@dataclass(frozen=True)
class HostConfigurationUnreadable:
    """The channel could not be read or written, and a later attempt may succeed.

    A refusal of its own rather than a flavour of the layer-shared
    `ReadUnavailable`/`WriteUnavailable`: this door names the channel's own
    fault by the channel's own word, `host-configuration-unreadable`, instead
    of the generic retry sentence every other store-backed route answers with.
    """

    detail: str | None = None


type GetProjectRootResult = (
    ProjectRootRead
    | ProjectRootMissing
    | ProjectRootProjectUnknown
    | HostConfigurationUnreadable
    | DurableStateCorrupt
)


@dataclass(frozen=True)
class ProjectRootRevisionPublished:
    revision: ProjectRootRevision


@dataclass(frozen=True)
class ProjectRootRevisionUnchanged:
    revision: ProjectRootRevision


@dataclass(frozen=True)
class ProjectRootRevisionConflict:
    pass


@dataclass(frozen=True)
class UnpublishableProjectRootRevision:
    """The authored fields do not make one project-root revision."""


type PublishProjectRootUseCaseResult = (
    ProjectRootRevisionPublished
    | ProjectRootRevisionUnchanged
    | ProjectRootRevisionConflict
    | ProjectRootProjectUnknown
    | UnpublishableProjectRootRevision
    | HostConfigurationUnreadable
    | DurableStateCorrupt
)


def get_project_root_revision(
    project_id: str,
    channel: HostConfigurationChannel,
) -> GetProjectRootResult:
    try:
        project = ProjectId(project_id)
    except ProjectUnknown:
        return ProjectRootProjectUnknown()
    match channel.latest_project_root_revision(project):
        case ProjectRootRevision() as revision:
            return ProjectRootRead(revision)
        case None:
            return ProjectRootMissing()
        case PortHostConfigurationReadUnavailable(detail):
            return HostConfigurationUnreadable(detail)
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)


def publish_project_root_revision(
    project_id: str,
    revision_number: int,
    root_path: str,
    channel: HostConfigurationChannel,
) -> PublishProjectRootUseCaseResult:
    try:
        project = ProjectId(project_id)
    except ProjectUnknown:
        return ProjectRootProjectUnknown()
    try:
        revision = ProjectRootRevision(project, revision_number, Path(root_path))
    except (TypeError, ValueError):
        return UnpublishableProjectRootRevision()
    match channel.publish_project_root_revision(revision):
        case PortProjectRootRevisionCreated(stored):
            return ProjectRootRevisionPublished(stored)
        case PortProjectRootRevisionExisting(stored):
            return ProjectRootRevisionUnchanged(stored)
        case PortProjectRootRevisionConflict():
            return ProjectRootRevisionConflict()
        case PortHostConfigurationReadUnavailable(detail):
            return HostConfigurationUnreadable(detail)
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
