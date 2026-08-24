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
from atelier2.contracts.host_configuration import (
    ConnectionActor,
    ProjectId,
    ProjectSourceConnectionRevision,
    SourceAddress,
    SourceConnectionAuthMethod,
    SourceKind,
)
from atelier2.ports.host_configuration import (
    ProjectSourceConnectionRevisionConflict,
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
