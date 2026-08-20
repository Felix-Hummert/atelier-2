"""Project resources expose only the server-generated public identity."""

from __future__ import annotations

from atelier2.api.references import encode_public_project_reference
from atelier2.api.wire.resources import ProjectListResource, ProjectResource
from atelier2.application.read_projects import ProjectListRead, ProjectRead


def project_resource(project: ProjectRead) -> ProjectResource:
    return ProjectResource(
        public_project_reference=encode_public_project_reference(project.project_id)
    )


def project_list_resource(projects: ProjectListRead) -> ProjectListResource:
    return ProjectListResource(
        items=tuple(project_resource(project) for project in projects.items)
    )
