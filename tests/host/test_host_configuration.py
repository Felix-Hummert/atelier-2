"""The host reads project id → root path from one live-versioned channel."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from atelier2.adapters.host_configuration import HostConfigurationChannel
from atelier2.contracts.host_configuration import (
    HOST_CONFIGURATION_CONFLICT,
    HOST_CONFIGURATION_UNREADABLE,
    PROJECT_UNKNOWN,
    HostProjectRootRevisionCreated,
    HostProjectRootRevisionExisting,
    ProjectId,
    ProjectRootFound,
    ProjectUnknown,
)
from atelier2.host import main
from atelier2.host.serving import compose_application
from tests.host.test_local_host import serve_arguments, served_settings


def project_root_document(
    project_id: str, revision_number: int, root_path: Path
) -> bytes:
    return json.dumps(
        {
            "project_id": project_id,
            "revision_number": revision_number,
            "root_path": str(root_path),
        }
    ).encode("utf-8")


def test_compose_reads_project_id_to_root_path_from_the_channel(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    channel_path = tmp_path / "host-configuration"
    channel_path.mkdir()
    published = HostConfigurationChannel(channel_path).publish(
        project_root_document("studio", 1, root)
    )

    assert isinstance(published, HostProjectRootRevisionCreated)

    settings = replace(served_settings(tmp_path), host_configuration_path=channel_path)
    _app, runtime = compose_application(settings)
    try:
        found = settings.project_root_for(ProjectId("studio"))
        assert isinstance(found, ProjectRootFound)
        assert found.root_path == root
        assert found.revision.revision_hash == published.revision.revision_hash
    finally:
        runtime.close()


def test_an_unknown_project_id_is_refused_by_name(tmp_path: Path) -> None:
    channel_path = tmp_path / "host-configuration"
    channel_path.mkdir()
    settings = replace(served_settings(tmp_path), host_configuration_path=channel_path)
    _app, runtime = compose_application(settings)
    try:
        missing = settings.project_root_for(ProjectId("unknown"))
        assert isinstance(missing, ProjectUnknown)
        assert PROJECT_UNKNOWN in str(missing)
    finally:
        runtime.close()


def test_the_latest_revision_is_the_mapping_compose_reads(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    channel_path = tmp_path / "host-configuration"
    channel_path.mkdir()
    channel = HostConfigurationChannel(channel_path)
    channel.publish(project_root_document("studio", 1, first))
    channel.publish(project_root_document("studio", 2, second))

    settings = replace(served_settings(tmp_path), host_configuration_path=channel_path)
    _app, runtime = compose_application(settings)
    try:
        found = settings.project_root_for(ProjectId("studio"))
        assert isinstance(found, ProjectRootFound)
        assert found.root_path == second
        assert found.revision.revision_number == 2
    finally:
        runtime.close()


def test_identical_bytes_are_the_same_revision(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    channel_path = tmp_path / "host-configuration"
    channel_path.mkdir()
    document = project_root_document("studio", 1, root)
    channel = HostConfigurationChannel(channel_path)

    first = channel.publish(document)
    second = channel.publish(document)

    assert isinstance(first, HostProjectRootRevisionCreated)
    assert isinstance(second, HostProjectRootRevisionExisting)
    assert first.revision.revision_hash == second.revision.revision_hash


def test_a_conflicting_revision_is_refused_by_name(tmp_path: Path) -> None:
    first = tmp_path / "first"
    other = tmp_path / "other"
    first.mkdir()
    other.mkdir()
    channel_path = tmp_path / "host-configuration"
    channel_path.mkdir()
    channel = HostConfigurationChannel(channel_path)
    channel.publish(project_root_document("studio", 1, first))

    conflict = channel.publish(project_root_document("studio", 1, other))

    assert HOST_CONFIGURATION_CONFLICT in str(conflict)


def test_a_declared_but_unreadable_channel_refuses_to_serve(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "no-such-channel"

    with pytest.raises(SystemExit) as refusal:
        main(serve_arguments(tmp_path, "--host-configuration", str(missing)))

    assert refusal.value.code == 2
    assert HOST_CONFIGURATION_UNREADABLE in capsys.readouterr().err
