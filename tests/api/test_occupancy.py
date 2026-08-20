from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from atelier2.api.app import create_app
from atelier2.api.limits import ApiLimits
from atelier2.api.openapi import API_PREFIX, OCCUPANCY_PATH
from atelier2.api.references import (
    MAXIMUM_PUBLIC_PROJECT_REFERENCE_CHARACTERS,
    encode_public_project_reference,
)
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


def _client(channel: RecordingChannel, limits: ApiLimits | None = None) -> TestClient:
    return TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=api_ports(host_configuration_channel=channel),
            limits=api_limits() if limits is None else limits,
            event_poll_backoff=event_poll_backoff(),
        )
    )


def _reference(project: str = PROJECT) -> str:
    return encode_public_project_reference(ProjectId(project))


def _path(project: str = PROJECT, lineage: str = LINEAGE) -> str:
    return OCCUPANCY_PATH.format(
        public_project_reference=_reference(project), lineage_id=lineage
    )


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
        "public_project_reference": _reference(),
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


def test_a_malformed_public_project_reference_is_named() -> None:
    client = _client(RecordingChannel(None))

    response = client.get(
        OCCUPANCY_PATH.format(public_project_reference="studio", lineage_id=LINEAGE)
    )

    assert response.status_code == 400
    assert response.json()["type"].endswith("invalid-public-project-reference")


def test_an_over_bound_public_project_reference_never_reaches_base64_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(RecordingChannel(None))

    def unexpected_decode(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError(
            "over-limit public project reference reached base64 decoding"
        )

    monkeypatch.setattr("atelier2.api.references.base64.b64decode", unexpected_decode)
    over_bound = "project1." + "A" * (
        MAXIMUM_PUBLIC_PROJECT_REFERENCE_CHARACTERS + 1 - len("project1.")
    )

    response = client.get(
        OCCUPANCY_PATH.format(public_project_reference=over_bound, lineage_id=LINEAGE)
    )

    assert response.status_code == 400
    assert response.json()["type"].endswith("invalid-public-project-reference")


def test_a_tight_field_limit_rejects_an_overlong_configured_project_id(
    tmp_path,
) -> None:
    project = "1234567890123"
    root = tmp_path / "project"
    root.mkdir()
    channel = RecordingChannel(ProjectRootRevision(ProjectId(project), 1, root))
    client = _client(channel, limits=api_limits(maximum_field_characters=12))

    response = client.get(_path(project=project))

    assert response.status_code == 400
    assert response.json()["type"].endswith("invalid-public-project-reference")
    assert channel.published == []


def test_the_maximum_project_id_round_trips_as_a_public_project_reference(
    tmp_path,
) -> None:
    project = "é" * MAXIMUM_PROJECT_ID_CHARACTERS
    root = tmp_path / "project"
    root.mkdir()
    channel = RecordingChannel(ProjectRootRevision(ProjectId(project), 1, root))
    client = _client(channel)
    revision = OccupancyRevision(
        ProjectId(project),
        CatalogLineageId(LINEAGE),
        1,
        (
            OccupancyBinding(
                AgentRole("chef"), AgentConfigurationRevisionHash(CONFIGURATION)
            ),
        ),
    )

    written = client.put(_path(project=project), json=_body())
    read = client.get(_path(project=project))

    assert written.status_code == 201
    assert read.status_code == 200
    assert written.json()["project_id"] == project
    assert written.json()["public_project_reference"] == _reference(project)
    assert written.json()["occupancy_revision_hash"] == revision.revision_hash.value
    assert read.json() == written.json()
    assert channel.published == [revision]


def test_a_malformed_lineage_id_is_catalog_lineage_missing(tmp_path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    client = _client(RecordingChannel(ProjectRootRevision(ProjectId(PROJECT), 1, root)))

    response = client.get(_path(lineage="not-a-lineage"))

    assert response.status_code == 404
    assert response.json()["type"].endswith("catalog-lineage-missing")


def test_a_slash_bearing_project_id_is_addressable_on_the_wire(tmp_path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    project = "team/red"
    revision = OccupancyRevision(
        ProjectId(project),
        CatalogLineageId(LINEAGE),
        1,
        (
            OccupancyBinding(
                AgentRole("chef"), AgentConfigurationRevisionHash(CONFIGURATION)
            ),
        ),
    )
    channel = RecordingChannel(ProjectRootRevision(ProjectId(project), 1, root))
    client = _client(channel)

    written = client.put(_path(project=project), json=_body())
    raw_slash = client.get(f"{API_PREFIX}/projects/{project}/occupancy/{LINEAGE}")

    assert written.status_code == 201
    assert written.json()["project_id"] == project
    assert written.json()["public_project_reference"] == _reference(project)
    assert written.json()["occupancy_revision_hash"] == revision.revision_hash.value
    assert raw_slash.status_code == 404
    assert raw_slash.json()["type"].endswith("route-not-found")


def test_one_hundred_occupancy_bindings_round_trip_and_one_more_is_refused(
    tmp_path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    channel = RecordingChannel(ProjectRootRevision(ProjectId(PROJECT), 1, root))
    client = _client(channel)
    configuration = CONFIGURATION
    hundred = [
        {
            "role": f"role{index}",
            "agent_configuration_revision_hash": configuration,
        }
        for index in range(100)
    ]

    accepted = client.put(_path(), json={"revision_number": 1, "bindings": hundred})
    refused = client.put(
        _path(),
        json={
            "revision_number": 2,
            "bindings": [
                *hundred,
                {
                    "role": "role100",
                    "agent_configuration_revision_hash": configuration,
                },
            ],
        },
    )

    assert accepted.status_code == 201
    assert len(accepted.json()["bindings"]) == 100
    assert client.get(_path()).json() == accepted.json()
    assert refused.status_code == 422
    assert refused.json()["type"].endswith("invalid-request")
    assert channel.published == [
        OccupancyRevision(
            ProjectId(PROJECT),
            CatalogLineageId(LINEAGE),
            1,
            tuple(
                OccupancyBinding(
                    AgentRole(f"role{index}"),
                    AgentConfigurationRevisionHash(configuration),
                )
                for index in range(100)
            ),
        )
    ]
