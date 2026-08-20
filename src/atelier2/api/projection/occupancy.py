"""Projection of occupancy revisions onto their wire schemas."""

from __future__ import annotations

from atelier2.api.references import encode_public_project_reference
from atelier2.api.wire.resources import (
    OccupancyBindingResource,
    OccupancyRevisionResource,
)
from atelier2.contracts.host_configuration import OccupancyRevision


def occupancy_revision_resource(
    revision: OccupancyRevision,
) -> OccupancyRevisionResource:
    return OccupancyRevisionResource(
        project_id=revision.project_id.value,
        public_project_reference=encode_public_project_reference(revision.project_id),
        lineage_id=revision.lineage_id.value,
        revision_number=revision.revision_number,
        occupancy_revision_hash=revision.revision_hash.value,
        bindings=tuple(
            OccupancyBindingResource(
                role=binding.role.value,
                agent_configuration_revision_hash=(
                    binding.agent_configuration_revision_hash.value
                ),
            )
            for binding in revision.bindings
        ),
    )
