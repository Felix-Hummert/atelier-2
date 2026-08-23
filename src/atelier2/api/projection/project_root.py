"""Projection of project-root revisions onto their wire schema."""

from __future__ import annotations

from atelier2.api.references import encode_public_project_reference
from atelier2.api.wire.resources import ProjectRootRevisionResource
from atelier2.contracts.host_configuration import ProjectRootRevision


def project_root_revision_resource(
    revision: ProjectRootRevision,
) -> ProjectRootRevisionResource:
    return ProjectRootRevisionResource(
        project_id=revision.project_id.value,
        public_project_reference=encode_public_project_reference(revision.project_id),
        revision_number=revision.revision_number,
        root_path=str(revision.root_path),
        project_root_revision_hash=revision.revision_hash.value,
    )
