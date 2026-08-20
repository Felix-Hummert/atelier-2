"""Reading and publishing recommended occupancy, as this layer's decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.refusals import (
    DurableStateCorrupt,
    ReadUnavailable,
    WriteUnavailable,
)
from atelier2.contracts.agents import AgentConfigurationRevisionHash, AgentRole
from atelier2.contracts.catalog_v3 import CatalogLineageId
from atelier2.contracts.host_configuration import (
    OccupancyBinding,
    OccupancyRevision,
    ProjectId,
    ProjectRootRevision,
    ProjectUnknown,
)
from atelier2.contracts.revisions_v3 import RevisionKind
from atelier2.ports.durable_runs import (
    DurableStateCorrupt as PortDurableStateCorrupt,
)
from atelier2.ports.durable_runs import DurableWriteUnavailable
from atelier2.ports.host_configuration import HostConfigurationChannel
from atelier2.ports.host_configuration import (
    HostConfigurationReadUnavailable as PortHostConfigurationReadUnavailable,
)
from atelier2.ports.host_configuration import (
    OccupancyRevisionCollision as PortOccupancyRevisionCollision,
)
from atelier2.ports.host_configuration import (
    OccupancyRevisionConflict as PortOccupancyRevisionConflict,
)
from atelier2.ports.host_configuration import (
    OccupancyRevisionCreated as PortOccupancyRevisionCreated,
)
from atelier2.ports.host_configuration import (
    OccupancyRevisionExisting as PortOccupancyRevisionExisting,
)
from atelier2.ports.published_revisions import (
    CatalogNameFound,
    CatalogNameMissing,
    CatalogResolver,
)


@dataclass(frozen=True)
class OccupancyRead:
    revision: OccupancyRevision


@dataclass(frozen=True)
class OccupancyMissing:
    pass


@dataclass(frozen=True)
class OccupancyProjectUnknown:
    pass


@dataclass(frozen=True)
class OccupancyLineageInvalid:
    pass


type GetOccupancyResult = (
    OccupancyRead
    | OccupancyMissing
    | OccupancyProjectUnknown
    | OccupancyLineageInvalid
    | ReadUnavailable
    | DurableStateCorrupt
)


@dataclass(frozen=True)
class OccupancyRevisionPublished:
    revision: OccupancyRevision


@dataclass(frozen=True)
class OccupancyRevisionUnchanged:
    revision: OccupancyRevision


@dataclass(frozen=True)
class OccupancyRevisionConflict:
    pass


@dataclass(frozen=True)
class OccupancyRevisionCollision:
    pass


@dataclass(frozen=True)
class UnpublishableOccupancy:
    """The authored values do not make one occupancy revision."""


type PublishOccupancyUseCaseResult = (
    OccupancyRevisionPublished
    | OccupancyRevisionUnchanged
    | OccupancyRevisionConflict
    | OccupancyRevisionCollision
    | OccupancyProjectUnknown
    | OccupancyLineageInvalid
    | UnpublishableOccupancy
    | WriteUnavailable
    | DurableStateCorrupt
)


def _known_project(
    project_id: ProjectId, channel: HostConfigurationChannel
) -> OccupancyProjectUnknown | ReadUnavailable | DurableStateCorrupt | None:
    match channel.latest_project_root_revision(project_id):
        case None:
            return OccupancyProjectUnknown()
        case ProjectRootRevision():
            return None
        case PortHostConfigurationReadUnavailable(detail):
            return ReadUnavailable(detail)
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)


def get_occupancy_revision(
    project_id: str,
    lineage_id: str,
    channel: HostConfigurationChannel,
) -> GetOccupancyResult:
    try:
        project = ProjectId(project_id)
    except ProjectUnknown:
        return OccupancyProjectUnknown()
    try:
        lineage = CatalogLineageId(lineage_id)
    except ValueError:
        return OccupancyLineageInvalid()
    known = _known_project(project, channel)
    if known is not None:
        return known
    match channel.latest_occupancy_revision(project, lineage):
        case OccupancyRevision() as revision:
            return OccupancyRead(revision)
        case None:
            return OccupancyMissing()
        case PortHostConfigurationReadUnavailable(detail):
            return ReadUnavailable(detail)
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)


def publish_occupancy_revision(
    project_id: str,
    lineage_id: str,
    revision_number: int,
    bindings: tuple[tuple[str, str], ...],
    channel: HostConfigurationChannel,
    catalog: CatalogResolver,
) -> PublishOccupancyUseCaseResult:
    try:
        project = ProjectId(project_id)
    except ProjectUnknown:
        return OccupancyProjectUnknown()
    try:
        lineage = CatalogLineageId(lineage_id)
    except ValueError:
        return OccupancyLineageInvalid()
    try:
        revision = OccupancyRevision(
            project,
            lineage,
            revision_number,
            tuple(
                OccupancyBinding(
                    AgentRole(role), AgentConfigurationRevisionHash(configuration)
                )
                for role, configuration in bindings
            ),
        )
    except (TypeError, ValueError):
        return UnpublishableOccupancy()
    known = _known_project(project, channel)
    if known is not None:
        if isinstance(known, ReadUnavailable):
            return WriteUnavailable(known.detail)
        return known
    match catalog.resolve_name(RevisionKind.WORKFLOW, lineage, "head"):
        case CatalogNameFound():
            pass
        case CatalogNameMissing():
            return OccupancyLineageInvalid()
        case _ as unreachable:
            assert_never(unreachable)
    match channel.publish_occupancy_revision(revision):
        case PortOccupancyRevisionCreated(stored):
            return OccupancyRevisionPublished(stored)
        case PortOccupancyRevisionExisting(stored):
            return OccupancyRevisionUnchanged(stored)
        case PortOccupancyRevisionConflict():
            return OccupancyRevisionConflict()
        case PortOccupancyRevisionCollision():
            return OccupancyRevisionCollision()
        case DurableWriteUnavailable():
            return WriteUnavailable()
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
