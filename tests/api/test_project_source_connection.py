from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atelier2.adapters.github.project_connections import GitHubProjectSourceConnector
from atelier2.api.app import create_app
from atelier2.api.openapi import PROJECT_SOURCE_CONNECTION_PATH
from atelier2.api.references import encode_public_project_reference
from atelier2.contracts.host_configuration import (
    ConnectionActor,
    ProjectId,
    ProjectRootRevision,
    ProjectSourceConnectionLifecycle,
    ProjectSourceConnectionRevision,
    ProjectSourceId,
    SourceAddress,
    SourceConnectionAuthMethod,
    SourceKind,
)
from atelier2.ports.durable_runs import DurableStateCorrupt
from atelier2.ports.host_configuration import HostConfigurationReadUnavailable
from tests.scenarios.api import api_limits, api_ports, event_poll_backoff

PROJECT = ProjectId("team/red")
REFERENCE = encode_public_project_reference(PROJECT)


@dataclass
class ConnectionChannel:
    result: object
    reads: list[ProjectId] = field(default_factory=list)

    def latest_project_source_connection_revision(
        self, project_id: ProjectId
    ) -> object:
        self.reads.append(project_id)
        return self.result


@dataclass
class ProjectChannel:
    result: object

    def latest_project_root_revision(self, project_id: ProjectId) -> object:
        assert project_id == PROJECT
        return self.result


def connection() -> ProjectSourceConnectionRevision:
    return ProjectSourceConnectionRevision(
        PROJECT,
        ProjectSourceId("11111111-1111-1111-1111-111111111111"),
        3,
        SourceKind("github"),
        SourceAddress("FlexOr2/atelier-2"),
        Path("/operator/credentials"),
        SourceConnectionAuthMethod.PERSONAL_ACCESS_TOKEN,
        ConnectionActor("operator"),
        ProjectSourceConnectionLifecycle.CONNECTED,
        None,
        None,
    )


def client(connection_channel: ConnectionChannel) -> TestClient:
    return TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=api_ports(
                host_configuration_channel=ProjectChannel(
                    ProjectRootRevision(PROJECT, 1, Path("/operator/project"))
                ),
                project_source_connection_channel=connection_channel,
                project_source_connector=GitHubProjectSourceConnector(),
            ),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
            served_project_id=PROJECT,
        )
    )


def path(reference: str = REFERENCE) -> str:
    return PROJECT_SOURCE_CONNECTION_PATH.format(public_project_reference=reference)


def test_get_reads_the_connection_without_credential_reference() -> None:
    revision = connection()

    response = client(ConnectionChannel(revision)).get(path())

    assert response.status_code == 200
    assert response.json() == {
        "public_project_reference": REFERENCE,
        "revision_number": 3,
        "source_kind": "github",
        "source_address": "FlexOr2/atelier-2",
        "auth_method": "personal-access-token",
        "project_source_connection_revision_hash": revision.revision_hash.value,
    }
    assert "credentials" not in response.text
    assert "operator" not in response.text


@pytest.mark.proves("a-project-source-identity-never-includes-a-branch")
def test_get_refuses_a_branch_stored_in_the_v45_identity() -> None:
    revision = connection()
    legacy = ProjectSourceConnectionRevision(
        revision.project_id,
        revision.source_id,
        revision.revision_number,
        revision.source_kind,
        SourceAddress("FlexOr2/atelier-2@legacy-main"),
        revision.credential_directory,
        revision.auth_method,
        revision.connected_by,
        revision.lifecycle,
        revision.connected_at,
        None,
    )

    response = client(ConnectionChannel(legacy)).get(path())

    assert response.status_code == 500
    assert response.json()["type"].endswith("durable-state-corrupt")


def test_get_names_an_absent_connection() -> None:
    response = client(ConnectionChannel(None)).get(path())

    assert response.status_code == 409
    assert response.json()["type"].endswith("project-source-not-connected")


def test_get_names_an_unavailable_connection() -> None:
    response = client(
        ConnectionChannel(HostConfigurationReadUnavailable("disk unavailable"))
    ).get(path())

    assert response.status_code == 503
    assert response.json()["type"].endswith("temporarily-unavailable")


def test_get_rejects_an_invalid_public_project_reference_before_reading() -> None:
    response = client(ConnectionChannel(DurableStateCorrupt())).get(
        path("not-a-reference")
    )

    assert response.status_code == 400
    assert response.json()["type"].endswith("invalid-public-project-reference")


def test_get_rejects_a_foreign_project_without_reading_the_connection_channel() -> None:
    connection_channel = ConnectionChannel(connection())
    foreign_reference = encode_public_project_reference(ProjectId("team/blue"))

    response = client(connection_channel).get(path(foreign_reference))

    assert response.status_code == 404
    assert response.json()["type"].endswith("project-unknown")
    assert connection_channel.reads == []
