"""Project-source projection without the credential-directory reference."""

from atelier2.api.references import encode_public_project_reference
from atelier2.api.wire.resources import ProjectSourceConnectionRevisionResource
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
