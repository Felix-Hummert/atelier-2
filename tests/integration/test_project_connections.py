"""Connecting a project to its source: identities recorded, credentials never.

The third family on the host configuration channel (#567, ADR 0010 decision 2):
an explicit connect appends one immutable revision, an unconnected project
answers `platform-connection-unknown`, and no flow ever moves a credential
value — proven by a canary token that must not surface anywhere.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from atelier2.adapters.dbos.host_configuration import (
    DbosHostConfigurationChannel,
    append_project_root,
)
from atelier2.adapters.dbos.schema import host_project_source_connection_revisions
from atelier2.application.project_connections import (
    ConnectionProjectUnknown,
    ConnectProjectSourceResult,
    PlatformConnectionUnknown,
    ProjectSourceConnectionPublished,
    ProjectSourceConnectionRead,
    ProjectSourceConnectionUnchanged,
    UnpublishableConnection,
    connect_project_source,
    get_project_source_connection,
)
from atelier2.application.refusals import DurableStateCorrupt
from atelier2.contracts.host_configuration import (
    ConnectionActor,
    ProjectId,
    ProjectSourceConnectionLifecycle,
    ProjectSourceConnectionRevision,
    ProjectSourceId,
    SourceAddress,
    SourceConnectionAuthMethod,
    SourceKind,
)
from atelier2.host import main
from atelier2.ports.host_configuration import (
    ProjectSourceConnectionRevisionConflict,
    ProjectSourceConnectionRevisionCreated,
)
from tests.integration.test_host_configuration import opened_channel

CANARY_TOKEN = "gho_atelier2_canary_token_must_not_appear"


@pytest.fixture
def connected_workshop(tmp_path: Path) -> Iterator[tuple[Engine, Path]]:
    """An opened channel with the project 'studio' rooted, plus a credential
    directory whose token file holds the canary."""

    root = tmp_path / "project"
    root.mkdir()
    credential_directory = tmp_path / "credential"
    credential_directory.mkdir()
    (credential_directory / "token").write_text(CANARY_TOKEN, encoding="utf-8")
    engine = opened_channel(tmp_path)
    append_project_root(engine, ProjectId("studio"), root)
    yield engine, credential_directory
    engine.dispose()


def _connect(
    engine: Engine,
    credential_directory: Path,
    *,
    project_id: str = "studio",
    source_address: str = "acme/studio",
) -> ConnectProjectSourceResult:
    channel = DbosHostConfigurationChannel(engine)
    return connect_project_source(
        project_id,
        "github",
        source_address,
        credential_directory,
        "personal-access-token",
        "felix",
        channel,
        channel,
    )


def test_connect_writes_one_readable_revision(
    connected_workshop: tuple[Engine, Path],
) -> None:
    engine, credential_directory = connected_workshop

    result = _connect(engine, credential_directory)

    assert isinstance(result, ProjectSourceConnectionPublished)
    assert result.revision.revision_number == 1
    read = get_project_source_connection("studio", DbosHostConfigurationChannel(engine))
    assert isinstance(read, ProjectSourceConnectionRead)
    assert read.revision == result.revision
    assert read.revision.source_kind == SourceKind("github")
    assert read.revision.source_address == SourceAddress("acme/studio")
    assert read.revision.credential_directory == credential_directory.resolve()
    assert read.revision.auth_method is (
        SourceConnectionAuthMethod.PERSONAL_ACCESS_TOKEN
    )
    assert read.revision.connected_by == ConnectionActor("felix")


def test_an_unconnected_project_answers_platform_connection_unknown(
    connected_workshop: tuple[Engine, Path],
) -> None:
    engine, _ = connected_workshop

    read = get_project_source_connection("studio", DbosHostConfigurationChannel(engine))

    assert read == PlatformConnectionUnknown()


def test_reconnecting_the_same_source_appends_nothing(
    connected_workshop: tuple[Engine, Path],
) -> None:
    engine, credential_directory = connected_workshop
    first = _connect(engine, credential_directory)
    assert isinstance(first, ProjectSourceConnectionPublished)

    again = _connect(engine, credential_directory)

    assert again == ProjectSourceConnectionUnchanged(first.revision)
    with engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(
                    host_project_source_connection_revisions
                )
            )
            == 1
        )


def test_reconnecting_a_different_source_appends_the_next_revision(
    connected_workshop: tuple[Engine, Path],
) -> None:
    engine, credential_directory = connected_workshop
    _connect(engine, credential_directory)

    moved = _connect(engine, credential_directory, source_address="acme/studio-mirror")

    assert isinstance(moved, ProjectSourceConnectionPublished)
    assert moved.revision.revision_number == 2
    read = get_project_source_connection("studio", DbosHostConfigurationChannel(engine))
    assert isinstance(read, ProjectSourceConnectionRead)
    assert read.revision.source_address == SourceAddress("acme/studio-mirror")


def test_connecting_a_project_without_a_root_is_refused(
    connected_workshop: tuple[Engine, Path],
) -> None:
    engine, credential_directory = connected_workshop

    result = _connect(engine, credential_directory, project_id="unrooted")

    assert result == ConnectionProjectUnknown()


def test_values_that_make_no_revision_are_unpublishable(
    connected_workshop: tuple[Engine, Path],
) -> None:
    engine, credential_directory = connected_workshop
    channel = DbosHostConfigurationChannel(engine)

    result = connect_project_source(
        "studio",
        "github",
        "acme/studio",
        credential_directory,
        "a-method-nobody-declared",
        "felix",
        channel,
        channel,
    )

    assert result == UnpublishableConnection()


def test_a_different_connection_at_the_same_key_conflicts(
    connected_workshop: tuple[Engine, Path],
) -> None:
    engine, credential_directory = connected_workshop
    first = _connect(engine, credential_directory)
    assert isinstance(first, ProjectSourceConnectionPublished)
    channel = DbosHostConfigurationChannel(engine)
    rival = ProjectSourceConnectionRevision(
        ProjectId("studio"),
        first.revision.source_id,
        1,
        SourceKind("github"),
        SourceAddress("acme/other"),
        credential_directory,
        SourceConnectionAuthMethod.PERSONAL_ACCESS_TOKEN,
        ConnectionActor("felix"),
    )

    assert (
        channel.publish_project_source_connection_revision(rival)
        == ProjectSourceConnectionRevisionConflict()
    )


def test_latest_connection_and_cli_identity_follow_the_active_source(
    connected_workshop: tuple[Engine, Path],
) -> None:
    engine, credential_directory = connected_workshop
    channel = DbosHostConfigurationChannel(engine)
    first_source = ProjectSourceId("11111111-1111-4111-8111-111111111111")
    active_source = ProjectSourceId("22222222-2222-4222-8222-222222222222")
    first_connected = ProjectSourceConnectionRevision(
        ProjectId("studio"),
        first_source,
        1,
        SourceKind("github"),
        SourceAddress("acme/retired"),
        credential_directory,
        SourceConnectionAuthMethod.PERSONAL_ACCESS_TOKEN,
        ConnectionActor("felix"),
    )
    first_disconnected = ProjectSourceConnectionRevision(
        ProjectId("studio"),
        first_source,
        2,
        first_connected.source_kind,
        first_connected.source_address,
        credential_directory,
        first_connected.auth_method,
        first_connected.connected_by,
        ProjectSourceConnectionLifecycle.DISCONNECTED,
    )
    second_connected = ProjectSourceConnectionRevision(
        ProjectId("studio"),
        active_source,
        1,
        SourceKind("github"),
        SourceAddress("acme/studio"),
        credential_directory,
        SourceConnectionAuthMethod.PERSONAL_ACCESS_TOKEN,
        ConnectionActor("felix"),
    )
    assert channel.publish_project_source_connection_revision(
        first_connected
    ) == ProjectSourceConnectionRevisionCreated(first_connected)
    assert channel.publish_project_source_connection_revision(
        first_disconnected
    ) == ProjectSourceConnectionRevisionCreated(first_disconnected)
    assert channel.publish_project_source_connection_revision(
        second_connected
    ) == ProjectSourceConnectionRevisionCreated(second_connected)

    read = get_project_source_connection("studio", channel)
    assert read == ProjectSourceConnectionRead(second_connected)

    result = _connect(engine, credential_directory)

    assert result == ProjectSourceConnectionUnchanged(second_connected)
    assert _connection_revision_count(engine) == 3


def test_reading_more_than_one_active_source_is_typed_durable_corruption(
    connected_workshop: tuple[Engine, Path],
) -> None:
    engine, credential_directory = connected_workshop
    revisions = tuple(
        ProjectSourceConnectionRevision(
            ProjectId("studio"),
            source_id,
            1,
            SourceKind("github"),
            SourceAddress(source_address),
            credential_directory,
            SourceConnectionAuthMethod.PERSONAL_ACCESS_TOKEN,
            ConnectionActor("felix"),
        )
        for source_id, source_address in (
            (
                ProjectSourceId("11111111-1111-4111-8111-111111111111"),
                "acme/first",
            ),
            (
                ProjectSourceId("22222222-2222-4222-8222-222222222222"),
                "acme/second",
            ),
        )
    )
    with engine.begin() as connection:
        connection.execute(
            host_project_source_connection_revisions.insert(),
            tuple(
                {
                    "revision_hash": revision.revision_hash.value,
                    "project_id": revision.project_id.value,
                    "source_id": revision.source_id.value,
                    "source_kind": revision.source_kind.value,
                    "revision_number": revision.revision_number,
                    "source_address": revision.source_address.value,
                    "credential_directory": str(revision.credential_directory),
                    "auth_method": revision.auth_method.value,
                    "connected_by": revision.connected_by.value,
                    "lifecycle": revision.lifecycle.value,
                    "connected_at": None,
                }
                for revision in revisions
            ),
        )

    assert (
        get_project_source_connection("studio", DbosHostConfigurationChannel(engine))
        == DurableStateCorrupt()
    )


def _connection_revision_count(engine: Engine) -> int:
    with engine.connect() as connection:
        return int(
            connection.scalar(
                sa.select(sa.func.count()).select_from(
                    host_project_source_connection_revisions
                )
            )
            or 0
        )


@pytest.mark.parametrize(
    "rewrite",
    [
        pytest.param(
            host_project_source_connection_revisions.update().values(
                source_address="acme/hijacked"
            ),
            id="update",
        ),
        pytest.param(host_project_source_connection_revisions.delete(), id="delete"),
    ],
)
def test_a_connection_revision_can_no_longer_be_rewritten(
    connected_workshop: tuple[Engine, Path], rewrite: sa.Executable
) -> None:
    engine, credential_directory = connected_workshop
    _connect(engine, credential_directory)

    with (
        pytest.raises(
            IntegrityError, match="project-source connection revisions are immutable"
        ),
        engine.begin() as connection,
    ):
        connection.execute(rewrite)


def test_no_flow_and_no_stored_byte_carries_the_credential_value(
    connected_workshop: tuple[Engine, Path], tmp_path: Path
) -> None:
    """The record is a reference: the canary the credential directory holds
    must appear in no revision, no read, and no row of the whole store."""

    engine, credential_directory = connected_workshop
    result = _connect(engine, credential_directory)
    read = get_project_source_connection("studio", DbosHostConfigurationChannel(engine))

    assert CANARY_TOKEN not in repr(result)
    assert CANARY_TOKEN not in repr(read)
    engine.dispose()
    with sqlite3.connect(tmp_path / "atelier.sqlite") as connection:
        full_projection = "\n".join(connection.iterdump())
    assert CANARY_TOKEN not in full_projection


def _connect_command(
    tmp_path: Path,
    credential_directory: Path,
    *,
    project_id: str = "studio",
) -> list[str]:
    return [
        "connect",
        "--database",
        str(tmp_path / "atelier.sqlite"),
        "--project-id",
        project_id,
        "--source-kind",
        "github",
        "--source-address",
        "acme/studio",
        "--credential-directory",
        str(credential_directory),
        "--auth-method",
        "personal-access-token",
        "--actor",
        "felix",
    ]


def test_the_connect_command_writes_the_revision_the_channel_reads_back(
    connected_workshop: tuple[Engine, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine, credential_directory = connected_workshop
    engine.dispose()

    assert main(_connect_command(tmp_path, credential_directory)) == 0
    assert "revision 1" in capsys.readouterr().out

    assert main(_connect_command(tmp_path, credential_directory)) == 0
    assert "unchanged" in capsys.readouterr().out

    reopened = opened_channel(tmp_path)
    try:
        read = get_project_source_connection(
            "studio", DbosHostConfigurationChannel(reopened)
        )
        assert isinstance(read, ProjectSourceConnectionRead)
        assert read.revision.revision_number == 1
        assert read.revision.source_address == SourceAddress("acme/studio")
    finally:
        reopened.dispose()


def test_the_connect_command_refuses_an_unrooted_project(
    connected_workshop: tuple[Engine, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine, credential_directory = connected_workshop
    engine.dispose()

    exit_code = main(
        _connect_command(tmp_path, credential_directory, project_id="unrooted")
    )

    assert exit_code == 1
    assert "project-unknown" in capsys.readouterr().err


def test_the_connect_command_does_not_create_a_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    credential_directory = tmp_path / "credential"
    credential_directory.mkdir()

    exit_code = main(_connect_command(tmp_path, credential_directory))

    assert exit_code == 1
    assert "does not create a store" in capsys.readouterr().err
    assert not (tmp_path / "atelier.sqlite").exists()
