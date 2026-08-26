"""Wire projections for exact model configuration and role resolution."""

from atelier2.api.references import encode_public_project_reference
from atelier2.api.wire.resources import (
    ModelRegistryEntryResource,
    ModelRegistryRevisionResource,
    ProjectModelDefaultResource,
    ProjectModelDefaultsRevisionResource,
    ProjectModelResolutionResource,
    RoleModelResolutionResource,
)
from atelier2.application.resolve_start_bindings import CastUnboundRolesResult
from atelier2.contracts.host_configuration import (
    ModelRegistryRevision,
    ProjectId,
    ProjectModelDefaultsRevision,
)
from atelier2.contracts.runs import WorkflowRevisionHash


def model_registry_resource(
    revision: ModelRegistryRevision,
) -> ModelRegistryRevisionResource:
    return ModelRegistryRevisionResource(
        provider_id=revision.provider_id.value,
        revision_number=revision.revision_number,
        model_registry_revision_hash=revision.revision_hash.value,
        entries=tuple(
            ModelRegistryEntryResource(
                model_id=entry.model_id,
                agent_configuration_revision_hash=(
                    entry.agent_configuration_revision_hash.value
                ),
                source=entry.source.value,
                provider_check=entry.provider_check.value,
            )
            for entry in revision.entries
        ),
    )


def project_model_defaults_resource(
    revision: ProjectModelDefaultsRevision,
) -> ProjectModelDefaultsRevisionResource:
    return ProjectModelDefaultsRevisionResource(
        project_id=revision.project_id.value,
        public_project_reference=encode_public_project_reference(revision.project_id),
        revision_number=revision.revision_number,
        project_model_defaults_revision_hash=revision.revision_hash.value,
        defaults=tuple(
            ProjectModelDefaultResource(
                difficulty=default.difficulty,
                model_registry_revision_hash=(
                    default.model_registry_revision_hash.value
                ),
                provider_id=default.provider_id.value,
                model_id=default.model_id,
                agent_configuration_revision_hash=(
                    default.agent_configuration_revision_hash.value
                ),
            )
            for default in revision.defaults
        ),
    )


def project_model_resolution_resource(
    project_id: ProjectId,
    workflow_revision_hash: WorkflowRevisionHash,
    resolution: CastUnboundRolesResult,
) -> ProjectModelResolutionResource:
    items: list[RoleModelResolutionResource] = []
    for item in resolution.resolutions:
        if item.declared_difficulty is None:
            raise ValueError("a V3 role resolution must carry its declared difficulty")
        items.append(
            RoleModelResolutionResource(
                role=item.role.value,
                agent_configuration_revision_hash=(
                    None
                    if item.agent_configuration_revision_hash is None
                    else item.agent_configuration_revision_hash.value
                ),
                source=item.source.value,
                model_id=item.model_id,
                declared_difficulty=item.declared_difficulty,
                default_difficulty=item.difficulty,
                uncast_reason=(
                    None if item.uncast_reason is None else item.uncast_reason.value
                ),
                family_differs_from=(
                    None
                    if item.family_differs_from is None
                    else item.family_differs_from.value
                ),
            )
        )
    return ProjectModelResolutionResource(
        project_id=project_id.value,
        public_project_reference=encode_public_project_reference(project_id),
        workflow_revision_hash=workflow_revision_hash.value,
        resolutions=tuple(items),
    )
