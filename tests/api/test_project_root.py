from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from atelier2.api.app import create_app
from atelier2.api.limits import ApiLimits
from atelier2.api.openapi import PROJECT_ROOT_PATH
from atelier2.api.references import encode_public_project_reference
from atelier2.application.project_root import (
    HostConfigurationUnreadable,
    ProjectRootProjectUnknown,
    ProjectRootRead,
    UnpublishableProjectRootRevision,
    get_project_root_revision,
    publish_project_root_revision,
)
from atelier2.application.refusals import DurableStateCorrupt
from atelier2.contracts.catalog_v3 import CatalogLineageId
from atelier2.contracts.host_configuration import (
    OccupancyRevision,
    ProjectId,
    ProjectRootRevision,
)
from atelier2.ports.durable_runs import DurableStateCorrupt as PortDurableStateCorrupt
from atelier2.ports.host_configuration import (
    HostConfigurationReadUnavailable,
    LatestOccupancyResult,
    LatestProjectRootResult,
    ProjectRootRevisionConflict,
    ProjectRootRevisionCreated,
    ProjectRootRevisionExisting,
    PublishOccupancyResult,
    PublishProjectRootResult,
)
from tests.scenarios.api import api_limits, api_ports, event_poll_backoff

PROJECT = "studio"


@dataclass
class RecordingChannel:
    """A full `HostConfigurationChannel`; only its project-root half is exercised.

    This suite calls `application.project_root` directly for the paths the HTTP
    surface cannot reach (a malformed raw project id, a corrupt durable read), so
    the fake must satisfy the whole protocol structurally even though its
    occupancy half never runs here.
    """

    root: ProjectRootRevision | None
    publish_result: PublishProjectRootResult | None = None
    read_result: LatestProjectRootResult | None = None
    published: list[ProjectRootRevision] = field(default_factory=list)

    def latest_project_root_revision(
        self, project_id: ProjectId
    ) -> LatestProjectRootResult:
        if self.read_result is not None:
            return self.read_result
        if self.root is None or self.root.project_id != project_id:
            return None
        return self.root

    def publish_project_root_revision(
        self, revision: ProjectRootRevision
    ) -> PublishProjectRootResult:
        self.published.append(revision)
        if self.publish_result is not None:
            return self.publish_result
        self.root = revision
        return ProjectRootRevisionCreated(revision)

    def latest_occupancy_revision(
        self, project_id: ProjectId, lineage_id: CatalogLineageId
    ) -> LatestOccupancyResult:
        raise AssertionError("test_project_root does not exercise occupancy")

    def publish_occupancy_revision(
        self, revision: OccupancyRevision
    ) -> PublishOccupancyResult:
        raise AssertionError("test_project_root does not exercise occupancy")


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


def _path(project: str = PROJECT) -> str:
    return PROJECT_ROOT_PATH.format(public_project_reference=_reference(project))


def _body(
    *, revision_number: int = 1, root_path: str = "/srv/studio"
) -> dict[str, object]:
    return {"revision_number": revision_number, "root_path": root_path}


def test_put_writes_a_project_root_revision_and_get_reads_it(tmp_path) -> None:
    root = str(tmp_path / "project")
    revision = ProjectRootRevision(ProjectId(PROJECT), 1, tmp_path / "project")
    channel = RecordingChannel(None)
    client = _client(channel)

    written = client.put(_path(), json=_body(root_path=root))
    read = client.get(_path())

    assert written.status_code == 201
    assert written.json() == {
        "project_id": PROJECT,
        "public_project_reference": _reference(),
        "revision_number": 1,
        "root_path": str(revision.root_path),
        "project_root_revision_hash": revision.revision_hash.value,
    }
    assert read.status_code == 200
    assert read.json() == written.json()
    assert channel.published == [revision]


def test_the_same_project_root_put_is_ok(tmp_path) -> None:
    root = tmp_path / "project"
    revision = ProjectRootRevision(ProjectId(PROJECT), 1, root)
    channel = RecordingChannel(
        revision, publish_result=ProjectRootRevisionExisting(revision)
    )
    client = _client(channel)

    response = client.put(_path(), json=_body(root_path=str(root)))

    assert response.status_code == 200
    assert response.json()["project_root_revision_hash"] == revision.revision_hash.value


def test_a_missing_project_root_is_named() -> None:
    client = _client(RecordingChannel(None))

    response = client.get(_path())

    assert response.status_code == 404
    assert response.json()["type"].endswith("project-root-missing")


def test_a_reused_revision_number_with_different_bytes_is_a_conflict(tmp_path) -> None:
    root = tmp_path / "project"
    channel = RecordingChannel(
        ProjectRootRevision(ProjectId(PROJECT), 1, root),
        publish_result=ProjectRootRevisionConflict(),
    )
    client = _client(channel)

    response = client.put(_path(), json=_body(root_path=str(tmp_path / "other")))

    assert response.status_code == 409
    assert response.json()["type"].endswith("project-root-revision-conflict")


def test_an_unreadable_channel_is_named_on_get() -> None:
    channel = RecordingChannel(
        None, read_result=HostConfigurationReadUnavailable("disk is gone")
    )
    client = _client(channel)

    response = client.get(_path())

    assert response.status_code == 503
    assert response.json()["type"].endswith("host-configuration-unreadable")


def test_an_unwritable_channel_is_named_on_put(tmp_path) -> None:
    channel = RecordingChannel(
        None,
        publish_result=HostConfigurationReadUnavailable("disk is gone"),
    )
    client = _client(channel)

    response = client.put(_path(), json=_body(root_path=str(tmp_path / "project")))

    assert response.status_code == 503
    assert response.json()["type"].endswith("host-configuration-unreadable")


def test_a_malformed_public_project_reference_is_named() -> None:
    client = _client(RecordingChannel(None))

    response = client.get(PROJECT_ROOT_PATH.format(public_project_reference="studio"))

    assert response.status_code == 400
    assert response.json()["type"].endswith("invalid-public-project-reference")


def test_a_slash_bearing_project_id_is_addressable_on_the_wire(tmp_path) -> None:
    project = "team/red"
    root = tmp_path / "project"
    revision = ProjectRootRevision(ProjectId(project), 1, root)
    channel = RecordingChannel(None)
    client = _client(channel)

    written = client.put(_path(project=project), json=_body(root_path=str(root)))

    assert written.status_code == 201
    assert written.json()["project_id"] == project
    assert written.json()["public_project_reference"] == _reference(project)
    assert channel.published == [revision]


@pytest.mark.parametrize("raw_project_id", ["", "\ud800"])
def test_a_malformed_raw_project_id_is_project_unknown(raw_project_id: str) -> None:
    """Reachable through the application layer, mirroring the store's own
    construction: the wire always addresses a project through the canonical,
    already-validated public reference, so a malformed raw id can only ever
    reach `ProjectId` construction the way the channel's own callers do.
    """
    channel = RecordingChannel(None)

    read = get_project_root_revision(raw_project_id, channel)
    written = publish_project_root_revision(raw_project_id, 1, "/srv/studio", channel)

    assert isinstance(read, ProjectRootProjectUnknown)
    assert isinstance(written, ProjectRootProjectUnknown)


def test_get_project_root_revision_reads_the_channels_latest_revision(tmp_path) -> None:
    root = tmp_path / "project"
    revision = ProjectRootRevision(ProjectId(PROJECT), 1, root)
    channel = RecordingChannel(revision)

    result = get_project_root_revision(PROJECT, channel)

    assert result == ProjectRootRead(revision)


def test_a_durable_state_corrupt_read_is_named() -> None:
    channel = RecordingChannel(None, read_result=PortDurableStateCorrupt())

    result = get_project_root_revision(PROJECT, channel)

    assert isinstance(result, DurableStateCorrupt)


def test_host_configuration_unreadable_carries_its_detail() -> None:
    channel = RecordingChannel(
        None, read_result=HostConfigurationReadUnavailable("disk is gone")
    )

    result = get_project_root_revision(PROJECT, channel)

    assert result == HostConfigurationUnreadable("disk is gone")


def test_an_out_of_bound_revision_number_is_unpublishable() -> None:
    channel = RecordingChannel(None)

    result = publish_project_root_revision(PROJECT, 0, "/srv/studio", channel)

    assert isinstance(result, UnpublishableProjectRootRevision)
    assert channel.published == []
