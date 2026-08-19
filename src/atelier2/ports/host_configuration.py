from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from atelier2.contracts.catalog_v3 import CatalogLineageId
from atelier2.contracts.host_configuration import (
    OccupancyRevision,
    ProjectId,
    ProjectRootRevision,
)
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable


@dataclass(frozen=True)
class HostConfigurationReadUnavailable:
    detail: str | None = None


@dataclass(frozen=True)
class OccupancyRevisionCreated:
    revision: OccupancyRevision


@dataclass(frozen=True)
class OccupancyRevisionExisting:
    revision: OccupancyRevision


@dataclass(frozen=True)
class OccupancyRevisionConflict:
    pass


@dataclass(frozen=True)
class OccupancyRevisionCollision:
    pass


type LatestProjectRootResult = (
    ProjectRootRevision | None | HostConfigurationReadUnavailable | DurableStateCorrupt
)

type LatestOccupancyResult = (
    OccupancyRevision | None | HostConfigurationReadUnavailable | DurableStateCorrupt
)

type PublishOccupancyResult = (
    OccupancyRevisionCreated
    | OccupancyRevisionExisting
    | OccupancyRevisionConflict
    | OccupancyRevisionCollision
    | DurableWriteUnavailable
    | DurableStateCorrupt
)


class HostConfigurationChannel(Protocol):
    def latest_project_root_revision(
        self, project_id: ProjectId
    ) -> LatestProjectRootResult: ...

    def latest_occupancy_revision(
        self, project_id: ProjectId, lineage_id: CatalogLineageId
    ) -> LatestOccupancyResult: ...

    def publish_occupancy_revision(
        self, revision: OccupancyRevision
    ) -> PublishOccupancyResult: ...
