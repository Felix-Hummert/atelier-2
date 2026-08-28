"""Project-source projection without the credential-directory reference."""

from __future__ import annotations

from atelier2.api.references import (
    encode_public_project_reference,
    encode_public_source_reference,
)
from atelier2.api.wire.resources import (
    ProjectSourceConnectionRevisionResource,
    ProjectSourceResource,
)
from atelier2.application.project_connections import ProjectSourceSummary
from atelier2.contracts.host_configuration import ProjectSourceConnectionRevision


def project_source_connection_revision_resource(
    revision: ProjectSourceConnectionRevision,
) -> ProjectSourceConnectionRevisionResource:
    return ProjectSourceConnectionRevisionResource(
        public_project_reference=encode_public_project_reference(revision.project_id),
        revision_number=revision.revision_number,
        source_kind=revision.source_kind.value,
        source_address=revision.source_address.value,
        auth_method=revision.auth_method,
        project_source_connection_revision_hash=revision.revision_hash.value,
    )


def project_source_resource(summary: ProjectSourceSummary) -> ProjectSourceResource:
    return ProjectSourceResource(
        public_source_reference=encode_public_source_reference(summary.source_id),
        kind=summary.source_kind.value,
        address=summary.public_address,
        connected_at=(
            None if summary.connected_at is None else summary.connected_at.value
        ),
        revision=summary.revision_number,
        auth_method=summary.auth_method,
    )
