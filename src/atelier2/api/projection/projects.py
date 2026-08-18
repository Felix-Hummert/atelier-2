"""Projection of a durable project onto its wire schema."""

from __future__ import annotations

from atelier2.api.wire.resources import ProjectResource
from atelier2.contracts.projects import Project


def project_resource(project: Project) -> ProjectResource:
    return ProjectResource(project_id=project.project_id.value, name=project.name.value)
