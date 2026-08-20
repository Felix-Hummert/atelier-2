from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atelier2.api.app import create_app
from atelier2.api.openapi import PROJECT_PATH, PROJECTS_PATH
from atelier2.api.references import (
    MAXIMUM_PUBLIC_PROJECT_REFERENCE_CHARACTERS,
    encode_public_project_reference,
)
from atelier2.contracts.host_configuration import ProjectId, ProjectRootRevision
from atelier2.ports.durable_runs import DurableStateCorrupt
from atelier2.ports.host_configuration import HostConfigurationReadUnavailable
from tests.scenarios.api import api_limits, api_ports, event_poll_backoff

PROJECT_ID = ProjectId("team/red")
PUBLIC_REFERENCE = encode_public_project_reference(PROJECT_ID)


@dataclass
class RecordingChannel:
    answer: object
    reads: list[ProjectId] = field(default_factory=list)

    def latest_project_root_revision(self, project_id: ProjectId) -> object:
        self.reads.append(project_id)
        return self.answer


def client(
    channel: RecordingChannel,
    served_project_id: ProjectId | None,
) -> TestClient:
    return TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=api_ports(host_configuration_channel=channel),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
            served_project_id=served_project_id,
        )
    )


def configured_channel(tmp_path: Path) -> RecordingChannel:
    return RecordingChannel(ProjectRootRevision(PROJECT_ID, 1, tmp_path / "secret"))


def test_an_instance_with_no_project_lists_an_honest_empty_collection() -> None:
    channel = RecordingChannel(None)

    response = client(channel, None).get(PROJECTS_PATH)

    assert response.status_code == 200
    assert response.json() == {"items": []}
    assert channel.reads == []


def test_the_list_and_its_delivered_detail_are_the_same_opaque_resource(
    tmp_path: Path,
) -> None:
    channel = configured_channel(tmp_path)
    http = client(channel, PROJECT_ID)

    listed = http.get(PROJECTS_PATH)
    detailed = http.get(PROJECT_PATH.format(public_project_reference=PUBLIC_REFERENCE))

    assert listed.status_code == 200
    assert listed.json() == {"items": [{"public_project_reference": PUBLIC_REFERENCE}]}
    assert detailed.status_code == 200
    assert detailed.json() == listed.json()["items"][0]
    exposed = repr((listed.json(), detailed.json()))
    assert PROJECT_ID.value not in exposed
    assert str((tmp_path / "secret").resolve()) not in exposed
    assert channel.reads == [PROJECT_ID, PROJECT_ID]


def test_a_well_formed_reference_for_another_project_is_unknown_without_a_read(
    tmp_path: Path,
) -> None:
    channel = configured_channel(tmp_path)
    other = encode_public_project_reference(ProjectId("other"))

    response = client(channel, PROJECT_ID).get(
        PROJECT_PATH.format(public_project_reference=other)
    )

    assert response.status_code == 404
    assert response.json()["type"].endswith("project-unknown")
    assert channel.reads == []


@pytest.mark.parametrize(
    "reference",
    [
        "studio",
        "project1.",
        "project1.@@",
        "x" * (MAXIMUM_PUBLIC_PROJECT_REFERENCE_CHARACTERS + 1),
    ],
)
def test_a_malformed_project_reference_is_invalid_before_the_channel_is_read(
    tmp_path: Path, reference: str
) -> None:
    channel = configured_channel(tmp_path)

    response = client(channel, PROJECT_ID).get(
        PROJECT_PATH.format(public_project_reference=reference)
    )

    assert response.status_code == 400
    assert response.json()["type"].endswith("invalid-public-project-reference")
    assert channel.reads == []


@pytest.mark.parametrize(
    ("answer", "status", "problem"),
    [
        (None, 404, "project-unknown"),
        (
            HostConfigurationReadUnavailable("channel offline"),
            503,
            "temporarily-unavailable",
        ),
        (DurableStateCorrupt(), 500, "durable-state-corrupt"),
    ],
)
def test_a_configured_project_read_failure_never_poses_as_an_empty_collection(
    answer: object, status: int, problem: str
) -> None:
    response = client(RecordingChannel(answer), PROJECT_ID).get(PROJECTS_PATH)

    assert response.status_code == status
    assert response.json()["type"].endswith(problem)
