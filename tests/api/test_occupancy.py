from __future__ import annotations

from dataclasses import dataclass, field

from fastapi.testclient import TestClient

from atelier2.api.app import create_app
from atelier2.api.openapi import OCCUPANCY_PATH
from atelier2.contracts.agents import AgentConfigurationRevisionHash, AgentRole
from atelier2.contracts.catalog_v3 import CatalogLineageId
from atelier2.contracts.host_configuration import (
    MAXIMUM_PROJECT_ID_CHARACTERS,
    OccupancyBinding,
    OccupancyRevision,
    ProjectId,
    ProjectRootRevision,
)
from atelier2.ports.host_configuration import (
    OccupancyRevisionCreated,
    OccupancyRevisionExisting,
)
from tests.scenarios.api import api_limits, api_ports, event_poll_backoff

LINEAGE = "ab" * 32
CONFIGURATION = "cd" * 32
PROJECT = "studio"


def _revision(
    *,
    revision_number: int = 1,
    configuration: str = CONFIGURATION,
) -> OccupancyRevision:
    return OccupancyRevision(
        ProjectId(PROJECT),
        CatalogLineageId(LINEAGE),
        revision_number,
        (
            OccupancyBinding(
                AgentRole("chef"), AgentConfigurationRevisionHash(configuration)
            ),
        ),
    )


@dataclass
class RecordingChannel:
    root: ProjectRootRevision | None
    occupancy: OccupancyRevision | None = None
    publish_result: object | None = None
    published: list[OccupancyRevision] = field(default_factory=list)

    def latest_project_root_revision(self, project_id: ProjectId) -> object:
        if self.root is None or self.root.project_id != project_id:
            return None
        return self.root

    def latest_occupancy_revision(
        self, project_id: ProjectId, lineage_id: CatalogLineageId
    ) -> object:
        if (
            self.occupancy is None
            or self.occupancy.project_id != project_id
            or self.occupancy.lineage_id != lineage_id
        ):
            return None
        return self.occupancy

    def publish_occupancy_revision(self, revision: OccupancyRevision) -> object:
        self.published.append(revision)
        self.occupancy = revision
        if self.publish_result is not None:
            return self.publish_result
        return OccupancyRevisionCreated(revision)


def _client(channel: RecordingChannel) -> TestClient:
    return TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=api_ports(host_configuration_channel=channel),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        )
    )


def _path(project: str = PROJECT, lineage: str = LINEAGE) -> str:
    return OCCUPANCY_PATH.format(project_id=project, lineage_id=lineage)


def _body(
    *, revision_number: int = 1, configuration: str = CONFIGURATION
) -> dict[str, object]:
    return {
        "revision_number": revision_number,
        "bindings": [
            {
                "role": "chef",
                "agent_configuration_revision_hash": configuration,
            }
        ],
    }


def test_put_writes_an_occupancy_revision_and_get_reads_it(tmp_path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    revision = _revision()
    channel = RecordingChannel(ProjectRootRevision(ProjectId(PROJECT), 1, root))
    client = _client(channel)

    written = client.put(_path(), json=_body())
    read = client.get(_path())

    assert written.status_code == 201
    assert written.json() == {
        "project_id": PROJECT,
        "lineage_id": LINEAGE,
        "revision_number": 1,
        "occupancy_revision_hash": revision.revision_hash.value,
        "bindings": [
            {
                "role": "chef",
                "agent_configuration_revision_hash": CONFIGURATION,
            }
        ],
    }
    assert read.status_code == 200
    assert read.json() == written.json()
    assert channel.published == [revision]


def test_the_same_occupancy_put_is_ok(tmp_path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    revision = _revision()
    channel = RecordingChannel(
        ProjectRootRevision(ProjectId(PROJECT), 1, root),
        publish_result=OccupancyRevisionExisting(revision),
    )
    client = _client(channel)

    response = client.put(_path(), json=_body())

    assert response.status_code == 200
    assert response.json()["occupancy_revision_hash"] == revision.revision_hash.value


def test_a_missing_occupancy_is_named(tmp_path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    client = _client(RecordingChannel(ProjectRootRevision(ProjectId(PROJECT), 1, root)))

    response = client.get(_path())

    assert response.status_code == 404
    assert response.json()["type"].endswith("occupancy-missing")


def test_an_unknown_project_is_project_unknown() -> None:
    client = _client(RecordingChannel(None))

    missing = client.get(_path())
    written = client.put(_path(), json=_body())

    assert missing.status_code == 404
    assert missing.json()["type"].endswith("project-unknown")
    assert written.status_code == 404
    assert written.json()["type"].endswith("project-unknown")


def test_a_malformed_project_id_is_project_unknown() -> None:
    client = _client(RecordingChannel(None))
    too_long = "x" * (MAXIMUM_PROJECT_ID_CHARACTERS + 1)

    response = client.get(_path(project=too_long))

    assert response.status_code == 404
    assert response.json()["type"].endswith("project-unknown")


def test_a_malformed_lineage_id_is_an_invalid_revision_hash(tmp_path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    client = _client(RecordingChannel(ProjectRootRevision(ProjectId(PROJECT), 1, root)))

    response = client.get(_path(lineage="not-a-lineage"))

    assert response.status_code == 400
    assert response.json()["type"].endswith("invalid-revision-hash")
