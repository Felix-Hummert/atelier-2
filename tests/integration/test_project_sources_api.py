"""The HTTP project-source doors against the real durable store."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Barrier
from typing import Literal

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.host_configuration import (
    DbosHostConfigurationChannel,
    append_project_root,
)
from atelier2.adapters.dbos.schema import host_project_source_connection_revisions
from atelier2.adapters.github.composition import GITHUB_SOURCE_KIND
from atelier2.adapters.github.live_effects import GITHUB_TOKEN_CREDENTIAL_ENTRY
from atelier2.adapters.github.project_connections import GitHubProjectSourceConnector
from atelier2.adapters.project_source_credentials import (
    FilesystemProjectSourceCredentialStore,
)
from atelier2.api.app import create_app
from atelier2.api.openapi import API_PREFIX
from atelier2.api.references import (
    encode_public_project_reference,
    encode_public_source_reference,
)
from atelier2.application.project_connections import (
    ManagedProjectSourcePublished,
    ProjectSourceAlreadyConnected,
    connect_managed_project_source,
)
from atelier2.contracts.host_configuration import (
    MAXIMUM_SOURCE_TOKEN_CHARACTERS,
    ConnectionActor,
    ProjectId,
    ProjectSourceConnectionLifecycle,
    ProjectSourceConnectionRevision,
    ProjectSourceId,
    SourceAddress,
    SourceConnectionAuthMethod,
    SourceKind,
    SourceReference,
)
from atelier2.contracts.when import RecordedAt
from atelier2.host import main
from atelier2.ports.durable_runs import DurableWriteUnavailable
from atelier2.ports.host_configuration import (
    LatestProjectSourceConnectionResult,
    LatestProjectSourceConnectionsResult,
    ProjectSourceConnectionChannel,
    ProjectSourceConnectionRevisionConflict,
    ProjectSourceCredentialDirectoryReferenceResult,
    PublishProjectSourceConnectionResult,
)
from atelier2.ports.project_connections import (
    ParsedProjectSourceAddress,
    ParseProjectSourceAddressResult,
    ProjectSourceAddressInvalid,
    ProjectSourceAuthenticationRefused,
    ProjectSourceConnector,
    ProjectSourceCredentialUnresolvable,
    ProjectSourceValidationUnavailable,
    ValidatedProjectSource,
    ValidateProjectSourceResult,
)
from tests.integration.test_host_configuration import opened_channel
from tests.integration.test_store_migration import _create_populated_v44_source_store
from tests.scenarios.api import api_limits, api_ports, event_poll_backoff

PROJECT = ProjectId("studio")
SOURCE = ProjectSourceId("11111111-1111-4111-8111-111111111111")
UNKNOWN_SOURCE = ProjectSourceId("22222222-2222-4222-8222-222222222222")
FIRST_CONNECTED_AT = RecordedAt("2026-08-28T08:00:00Z")
RECONNECTED_AT = RecordedAt("2026-08-28T09:00:00Z")


class FakeGitHubProjectSourceConnector:
    """Deterministic provider behavior at the real application boundary."""

    def parse_address(self, address: str) -> ParseProjectSourceAddressResult:
        prefix = "github.com/"
        if not address.startswith(prefix):
            return ProjectSourceAddressInvalid(
                "a GitHub source address must read github.com/owner/name"
            )
        public_address = address.removeprefix(prefix)
        if public_address.count("/") != 1:
            return ProjectSourceAddressInvalid(
                "a GitHub source address must read github.com/owner/name"
            )
        return ParsedProjectSourceAddress(GITHUB_SOURCE_KIND, public_address)

    def validate(
        self,
        parsed: ParsedProjectSourceAddress,
        credential_directory: Path,
    ) -> ValidateProjectSourceResult:
        token = (credential_directory / GITHUB_TOKEN_CREDENTIAL_ENTRY).read_text(
            encoding="utf-8"
        )
        if token == "refused-token":
            return ProjectSourceAuthenticationRefused(
                "Bad credentials from the fake provider"
            )
        if token == "provider-echo-token-canary":
            return ProjectSourceAuthenticationRefused(f"provider refused {token}")
        if token == "unavailable-token":
            return ProjectSourceValidationUnavailable("provider network unavailable")
        if token == "unresolvable-token":
            return ProjectSourceCredentialUnresolvable()
        return ValidatedProjectSource(
            parsed.source_kind,
            SourceAddress(parsed.public_address),
            SourceReference("main"),
            parsed.public_address,
        )

    def parse_stored_address(
        self, source_address: SourceAddress
    ) -> ParseProjectSourceAddressResult:
        try:
            public_address = self.public_address(source_address)
        except ValueError:
            return ProjectSourceAddressInvalid("the stored source is malformed")
        return ParsedProjectSourceAddress(GITHUB_SOURCE_KIND, public_address)

    def public_address(self, source_address: SourceAddress) -> str:
        public_address, separator, branch = source_address.value.partition("@")
        if separator and not branch:
            raise ValueError("the stored source address has an empty branch")
        return public_address


def _sequence(values: tuple[str, ...]) -> Callable[[], str]:
    remaining = iter(values)
    return lambda: next(remaining)


def _clock(values: tuple[RecordedAt, ...]) -> Callable[[], RecordedAt]:
    remaining = iter(values)
    return lambda: next(remaining)


def _client(
    engine: Engine,
    managed_root: Path,
    *,
    deposit_names: tuple[str, ...],
    clock_values: tuple[RecordedAt, ...] = (FIRST_CONNECTED_AT, RECONNECTED_AT),
    source_connector: ProjectSourceConnector | None = None,
    connection_channel: ProjectSourceConnectionChannel | None = None,
) -> TestClient:
    channel = DbosHostConfigurationChannel(engine)
    return TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=api_ports(
                host_configuration_channel=channel,
                project_source_connection_channel=(
                    channel if connection_channel is None else connection_channel
                ),
                project_source_connector=(
                    FakeGitHubProjectSourceConnector()
                    if source_connector is None
                    else source_connector
                ),
                project_source_credential_store=FilesystemProjectSourceCredentialStore(
                    managed_root, deposit_name=_sequence(deposit_names)
                ),
            ),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
            served_project_id=PROJECT,
            source_id_generator=lambda: SOURCE,
            connection_clock=_clock(clock_values),
        )
    )


@dataclass
class InterruptedProjectSourceWrites:
    delegate: DbosHostConfigurationChannel
    interruption: Literal["before", "after", "raise-after"]
    revision_number: int

    def latest_project_source_connection_revision(
        self, project_id: ProjectId
    ) -> LatestProjectSourceConnectionResult:
        return self.delegate.latest_project_source_connection_revision(project_id)

    def latest_project_source_connection_revisions(
        self, project_id: ProjectId
    ) -> LatestProjectSourceConnectionsResult:
        return self.delegate.latest_project_source_connection_revisions(project_id)

    def latest_project_source_connection_revision_by_source(
        self, project_id: ProjectId, source_id: ProjectSourceId
    ) -> LatestProjectSourceConnectionResult:
        return self.delegate.latest_project_source_connection_revision_by_source(
            project_id, source_id
        )

    def project_source_credential_directory_reference(
        self, project_id: ProjectId, credential_directory: Path
    ) -> ProjectSourceCredentialDirectoryReferenceResult:
        return self.delegate.project_source_credential_directory_reference(
            project_id, credential_directory
        )

    def publish_project_source_connection_revision(
        self, revision: ProjectSourceConnectionRevision
    ) -> PublishProjectSourceConnectionResult:
        if revision.revision_number != self.revision_number:
            return self.delegate.publish_project_source_connection_revision(revision)
        if self.interruption in ("after", "raise-after"):
            self.delegate.publish_project_source_connection_revision(revision)
        if self.interruption == "raise-after":
            raise OSError("the write result was lost")
        return DurableWriteUnavailable()


@dataclass
class ConflictingProjectSourceRotation:
    delegate: DbosHostConfigurationChannel
    injected: bool = False

    def latest_project_source_connection_revision(
        self, project_id: ProjectId
    ) -> LatestProjectSourceConnectionResult:
        return self.delegate.latest_project_source_connection_revision(project_id)

    def latest_project_source_connection_revisions(
        self, project_id: ProjectId
    ) -> LatestProjectSourceConnectionsResult:
        return self.delegate.latest_project_source_connection_revisions(project_id)

    def latest_project_source_connection_revision_by_source(
        self, project_id: ProjectId, source_id: ProjectSourceId
    ) -> LatestProjectSourceConnectionResult:
        return self.delegate.latest_project_source_connection_revision_by_source(
            project_id, source_id
        )

    def project_source_credential_directory_reference(
        self, project_id: ProjectId, credential_directory: Path
    ) -> ProjectSourceCredentialDirectoryReferenceResult:
        return self.delegate.project_source_credential_directory_reference(
            project_id, credential_directory
        )

    def publish_project_source_connection_revision(
        self, revision: ProjectSourceConnectionRevision
    ) -> PublishProjectSourceConnectionResult:
        if revision.revision_number != 2 or self.injected:
            return self.delegate.publish_project_source_connection_revision(revision)
        self.injected = True
        latest = self.delegate.latest_project_source_connection_revision_by_source(
            revision.project_id, revision.source_id
        )
        assert isinstance(latest, ProjectSourceConnectionRevision)
        rival = ProjectSourceConnectionRevision(
            latest.project_id,
            latest.source_id,
            2,
            latest.source_kind,
            latest.source_address,
            latest.credential_directory,
            latest.auth_method,
            latest.connected_by,
            latest.lifecycle,
            latest.connected_at,
            SourceReference("concurrent-ref"),
        )
        self.delegate.publish_project_source_connection_revision(rival)
        return ProjectSourceConnectionRevisionConflict()


@dataclass
class PublishedProjectSourceConflict:
    delegate: DbosHostConfigurationChannel

    def latest_project_source_connection_revision(
        self, project_id: ProjectId
    ) -> LatestProjectSourceConnectionResult:
        return self.delegate.latest_project_source_connection_revision(project_id)

    def latest_project_source_connection_revisions(
        self, project_id: ProjectId
    ) -> LatestProjectSourceConnectionsResult:
        return self.delegate.latest_project_source_connection_revisions(project_id)

    def latest_project_source_connection_revision_by_source(
        self, project_id: ProjectId, source_id: ProjectSourceId
    ) -> LatestProjectSourceConnectionResult:
        return self.delegate.latest_project_source_connection_revision_by_source(
            project_id, source_id
        )

    def project_source_credential_directory_reference(
        self, project_id: ProjectId, credential_directory: Path
    ) -> ProjectSourceCredentialDirectoryReferenceResult:
        return self.delegate.project_source_credential_directory_reference(
            project_id, credential_directory
        )

    def publish_project_source_connection_revision(
        self, revision: ProjectSourceConnectionRevision
    ) -> PublishProjectSourceConnectionResult:
        self.delegate.publish_project_source_connection_revision(revision)
        return ProjectSourceConnectionRevisionConflict()


@dataclass
class OvertakingProjectSourceWrite:
    delegate: DbosHostConfigurationChannel
    target_revision: int
    candidate_lands: bool
    winner_directory: Path
    injected: bool = False

    def latest_project_source_connection_revision(
        self, project_id: ProjectId
    ) -> LatestProjectSourceConnectionResult:
        return self.delegate.latest_project_source_connection_revision(project_id)

    def latest_project_source_connection_revisions(
        self, project_id: ProjectId
    ) -> LatestProjectSourceConnectionsResult:
        return self.delegate.latest_project_source_connection_revisions(project_id)

    def latest_project_source_connection_revision_by_source(
        self, project_id: ProjectId, source_id: ProjectSourceId
    ) -> LatestProjectSourceConnectionResult:
        return self.delegate.latest_project_source_connection_revision_by_source(
            project_id, source_id
        )

    def project_source_credential_directory_reference(
        self, project_id: ProjectId, credential_directory: Path
    ) -> ProjectSourceCredentialDirectoryReferenceResult:
        return self.delegate.project_source_credential_directory_reference(
            project_id, credential_directory
        )

    def publish_project_source_connection_revision(
        self, revision: ProjectSourceConnectionRevision
    ) -> PublishProjectSourceConnectionResult:
        if revision.revision_number != self.target_revision or self.injected:
            return self.delegate.publish_project_source_connection_revision(revision)
        self.injected = True
        if self.candidate_lands:
            self.delegate.publish_project_source_connection_revision(revision)
        winner = ProjectSourceConnectionRevision(
            revision.project_id,
            revision.source_id,
            revision.revision_number + 1,
            revision.source_kind,
            revision.source_address,
            self.winner_directory,
            revision.auth_method,
            revision.connected_by,
            revision.lifecycle,
            revision.connected_at,
            SourceReference("winner-ref"),
        )
        self.delegate.publish_project_source_connection_revision(winner)
        return ProjectSourceConnectionRevisionConflict()


@dataclass
class SecondConflictOvertakingRotation:
    delegate: DbosHostConfigurationChannel
    candidate_lands: bool
    winner_directory: Path

    def latest_project_source_connection_revision(
        self, project_id: ProjectId
    ) -> LatestProjectSourceConnectionResult:
        return self.delegate.latest_project_source_connection_revision(project_id)

    def latest_project_source_connection_revisions(
        self, project_id: ProjectId
    ) -> LatestProjectSourceConnectionsResult:
        return self.delegate.latest_project_source_connection_revisions(project_id)

    def latest_project_source_connection_revision_by_source(
        self, project_id: ProjectId, source_id: ProjectSourceId
    ) -> LatestProjectSourceConnectionResult:
        return self.delegate.latest_project_source_connection_revision_by_source(
            project_id, source_id
        )

    def project_source_credential_directory_reference(
        self, project_id: ProjectId, credential_directory: Path
    ) -> ProjectSourceCredentialDirectoryReferenceResult:
        return self.delegate.project_source_credential_directory_reference(
            project_id, credential_directory
        )

    def publish_project_source_connection_revision(
        self, revision: ProjectSourceConnectionRevision
    ) -> PublishProjectSourceConnectionResult:
        latest = self.delegate.latest_project_source_connection_revision_by_source(
            revision.project_id, revision.source_id
        )
        assert isinstance(latest, ProjectSourceConnectionRevision)
        if revision.revision_number == 2:
            rival = ProjectSourceConnectionRevision(
                latest.project_id,
                latest.source_id,
                2,
                latest.source_kind,
                latest.source_address,
                latest.credential_directory,
                latest.auth_method,
                latest.connected_by,
                latest.lifecycle,
                latest.connected_at,
                SourceReference("first-conflict"),
            )
            self.delegate.publish_project_source_connection_revision(rival)
            return ProjectSourceConnectionRevisionConflict()
        if revision.revision_number == 3:
            if self.candidate_lands:
                self.delegate.publish_project_source_connection_revision(revision)
            winner = ProjectSourceConnectionRevision(
                revision.project_id,
                revision.source_id,
                4,
                revision.source_kind,
                revision.source_address,
                self.winner_directory,
                revision.auth_method,
                revision.connected_by,
                revision.lifecycle,
                revision.connected_at,
                SourceReference("second-conflict-winner"),
            )
            self.delegate.publish_project_source_connection_revision(winner)
            return ProjectSourceConnectionRevisionConflict()
        return self.delegate.publish_project_source_connection_revision(revision)


def _source_paths() -> tuple[str, str, str, str]:
    project_reference = encode_public_project_reference(PROJECT)
    source_reference = encode_public_source_reference(SOURCE)
    collection = f"{API_PREFIX}/projects/{project_reference}/sources"
    member = f"{collection}/{source_reference}"
    return collection, member, f"{member}/token", source_reference


def _opened_project(tmp_path: Path) -> Engine:
    engine = opened_channel(tmp_path)
    project_root = tmp_path / "project"
    project_root.mkdir()
    append_project_root(engine, PROJECT, project_root)
    return engine


def _revision_count(engine: Engine) -> int:
    with engine.connect() as connection:
        return int(
            connection.scalar(
                sa.select(sa.func.count()).select_from(
                    host_project_source_connection_revisions
                )
            )
            or 0
        )


def _write_credential_directory(directory: Path, token: str) -> None:
    directory.mkdir(parents=True)
    (directory / GITHUB_TOKEN_CREDENTIAL_ENTRY).write_text(token, encoding="utf-8")


def _referenced_credential_directories(engine: Engine) -> frozenset[Path]:
    with engine.connect() as connection:
        return frozenset(
            Path(value)
            for value in connection.scalars(
                sa.select(
                    host_project_source_connection_revisions.c.credential_directory
                ).where(
                    host_project_source_connection_revisions.c.project_id
                    == PROJECT.value
                )
            )
        )


@pytest.mark.proves("a-project-source-identity-never-includes-a-branch")
@pytest.mark.proves("a-project-source-token-never-returns-from-its-http-door")
@pytest.mark.proves("disconnect-is-idempotent-and-every-reader-follows-lifecycle")
def test_connect_duplicate_disconnect_reconnect_and_rotate_through_real_app(
    tmp_path: Path,
) -> None:
    engine = _opened_project(tmp_path)
    managed_root = tmp_path / "managed-credentials"
    client = _client(
        engine,
        managed_root,
        deposit_names=("first", "reconnect", "rotated"),
    )
    collection, member, token_path, source_reference = _source_paths()

    connected = client.post(
        collection,
        json={"address": "github.com/acme/studio", "token": "first-token"},
    )

    assert connected.status_code == 201
    assert connected.json() == {
        "public_source_reference": source_reference,
        "kind": "github",
        "address": "acme/studio",
        "scope": "issues",
        "connected_at": FIRST_CONNECTED_AT.value,
        "revision": 1,
        "auth_method": "personal-access-token",
    }
    assert "first-token" not in connected.text
    assert "@main" not in connected.text
    assert _revision_count(engine) == 1
    published_deposits = tuple(managed_root.iterdir())
    assert len(published_deposits) == 1

    duplicate = client.post(
        collection,
        json={"address": "github.com/acme/studio", "token": "second-token"},
    )

    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == (
        "Disconnect the existing source before connecting another one."
    )
    assert tuple(managed_root.iterdir()) == published_deposits
    assert _revision_count(engine) == 1

    listed = client.get(collection)
    assert listed.status_code == 200
    assert listed.json() == {"items": [connected.json()]}
    assert "first-token" not in listed.text
    assert "credential" not in listed.text
    assert "@main" not in listed.text

    assert client.delete(member).status_code == 204
    assert _revision_count(engine) == 2
    assert client.delete(member).status_code == 204
    assert _revision_count(engine) == 2
    assert client.get(collection).json() == {"items": []}
    residual = f"{API_PREFIX}/projects/{encode_public_project_reference(PROJECT)}/source-connection"
    disconnected_residual = client.get(residual)
    assert disconnected_residual.status_code == 409
    assert disconnected_residual.json()["type"].endswith("project-source-not-connected")

    reconnected = client.post(
        collection,
        json={"address": "github.com/acme/studio", "token": "reconnect-token"},
    )
    assert reconnected.status_code == 201
    assert reconnected.json()["public_source_reference"] == source_reference
    assert reconnected.json()["connected_at"] == RECONNECTED_AT.value
    assert reconnected.json()["revision"] == 3

    rotated = client.put(token_path, json={"token": "rotated-token"})
    assert rotated.status_code == 200
    assert rotated.json()["public_source_reference"] == source_reference
    assert rotated.json()["connected_at"] == RECONNECTED_AT.value
    assert rotated.json()["revision"] == 4
    assert "rotated-token" not in rotated.text
    assert _revision_count(engine) == 4

    latest = DbosHostConfigurationChannel(
        engine
    ).latest_project_source_connection_revision_by_source(PROJECT, SOURCE)
    assert isinstance(latest, ProjectSourceConnectionRevision)
    assert latest.lifecycle is ProjectSourceConnectionLifecycle.CONNECTED
    assert latest.connected_at == RECONNECTED_AT
    assert latest.source_address == SourceAddress("acme/studio")
    assert latest.source_ref == SourceReference("main")
    assert (latest.credential_directory / GITHUB_TOKEN_CREDENTIAL_ENTRY).read_text(
        encoding="utf-8"
    ) == "rotated-token"

    engine.dispose()
    with sqlite3.connect(tmp_path / "atelier.sqlite") as connection:
        durable_bytes = "\n".join(connection.iterdump())
    for token in ("first-token", "second-token", "reconnect-token", "rotated-token"):
        assert token not in durable_bytes


@pytest.mark.proves("legacy-connection-unknowns-stay-null-and-private")
def test_a_migrated_v44_connection_keeps_its_unknown_instant_and_branch_private(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "atelier.sqlite"
    _create_populated_v44_source_store(database)
    assert main(["migrate", "--database", str(database)]) == 0
    assert "step 44 -> 45" in capsys.readouterr().out
    engine = opened_channel(tmp_path)
    project_root = tmp_path / "project"
    project_root.mkdir()
    append_project_root(engine, PROJECT, project_root)
    client = _client(
        engine,
        tmp_path / "managed-credentials",
        deposit_names=(),
    )

    listed = client.get(_source_paths()[0])
    residual_path = (
        f"{API_PREFIX}/projects/{encode_public_project_reference(PROJECT)}"
        "/source-connection"
    )
    residual = client.get(residual_path)

    assert listed.status_code == 200
    assert listed.json()["items"][0]["connected_at"] is None
    assert listed.json()["items"][0]["address"] == "acme/studio"
    assert "trunk" not in listed.text
    assert residual.status_code == 200
    assert residual.json()["source_address"] == "acme/studio"
    assert "trunk" not in residual.text
    engine.dispose()


def test_unknown_source_and_provider_refusals_are_typed_and_leave_state_unchanged(
    tmp_path: Path,
) -> None:
    engine = _opened_project(tmp_path)
    managed_root = tmp_path / "managed-credentials"
    client = _client(
        engine,
        managed_root,
        deposit_names=("refused", "unavailable", "accepted", "rotationrefused"),
    )
    collection, _, token_path, _ = _source_paths()
    unknown_reference = encode_public_source_reference(UNKNOWN_SOURCE)
    unknown_member = f"{collection}/{unknown_reference}"

    assert client.delete(unknown_member).status_code == 404
    assert (
        client.put(f"{unknown_member}/token", json={"token": "token"}).status_code
        == 404
    )

    malformed = client.post(
        collection, json={"address": "acme/studio", "token": "token"}
    )
    assert malformed.status_code == 422

    refused = client.post(
        collection,
        json={"address": "github.com/acme/studio", "token": "refused-token"},
    )
    assert refused.status_code == 422
    assert refused.json()["detail"] == (
        "Use a token the provider accepts for this source."
    )
    assert _revision_count(engine) == 0
    assert tuple(managed_root.iterdir()) == ()

    unavailable = client.post(
        collection,
        json={"address": "github.com/acme/studio", "token": "unavailable-token"},
    )
    assert unavailable.status_code == 503
    assert _revision_count(engine) == 0
    assert tuple(managed_root.iterdir()) == ()

    assert (
        client.post(
            collection,
            json={"address": "github.com/acme/studio", "token": "accepted-token"},
        ).status_code
        == 201
    )
    latest_before_failure = DbosHostConfigurationChannel(
        engine
    ).latest_project_source_connection_revision_by_source(PROJECT, SOURCE)
    assert isinstance(latest_before_failure, ProjectSourceConnectionRevision)

    rotation_refused = client.put(token_path, json={"token": "refused-token"})
    assert rotation_refused.status_code == 422
    assert _revision_count(engine) == 1
    latest_after_failure = DbosHostConfigurationChannel(
        engine
    ).latest_project_source_connection_revision_by_source(PROJECT, SOURCE)
    assert isinstance(latest_after_failure, ProjectSourceConnectionRevision)
    assert latest_after_failure == latest_before_failure
    assert (
        latest_after_failure.credential_directory / GITHUB_TOKEN_CREDENTIAL_ENTRY
    ).read_text(encoding="utf-8") == "accepted-token"
    assert len(tuple(managed_root.iterdir())) == 1
    engine.dispose()


@pytest.mark.proves("credential-publication-follows-the-durable-source-revision")
def test_an_unresolvable_staged_credential_is_fixed_unavailable_and_preserves_rotation(
    tmp_path: Path,
) -> None:
    engine = _opened_project(tmp_path)
    managed_root = tmp_path / "managed-credentials"
    client = _client(
        engine,
        managed_root,
        deposit_names=("missingconnect", "first", "missingrotate"),
    )
    collection, _, token_path, _ = _source_paths()

    refused_connect = client.post(
        collection,
        json={
            "address": "github.com/acme/studio",
            "token": "unresolvable-token",
        },
    )

    assert refused_connect.status_code == 503
    assert refused_connect.json()["detail"] == (
        "The connected platform did not answer; retry after it becomes reachable."
    )
    assert "unresolvable-token" not in refused_connect.text
    assert tuple(managed_root.iterdir()) == ()

    assert (
        client.post(
            collection,
            json={"address": "github.com/acme/studio", "token": "first-token"},
        ).status_code
        == 201
    )
    channel = DbosHostConfigurationChannel(engine)
    before = channel.latest_project_source_connection_revision_by_source(
        PROJECT, SOURCE
    )
    assert isinstance(before, ProjectSourceConnectionRevision)

    refused_rotate = client.put(token_path, json={"token": "unresolvable-token"})

    assert refused_rotate.status_code == 503
    assert refused_rotate.json()["detail"] == (
        "The connected platform did not answer; retry after it becomes reachable."
    )
    assert "unresolvable-token" not in refused_rotate.text
    assert (
        channel.latest_project_source_connection_revision_by_source(PROJECT, SOURCE)
        == before
    )
    assert (before.credential_directory / GITHUB_TOKEN_CREDENTIAL_ENTRY).read_text(
        encoding="utf-8"
    ) == "first-token"
    assert tuple(path.name for path in managed_root.iterdir()) == (
        f"{SOURCE.value}-first",
    )
    engine.dispose()


@pytest.mark.proves("disconnect-is-idempotent-and-every-reader-follows-lifecycle")
def test_disconnect_reconciles_a_conflict_that_already_published_the_release(
    tmp_path: Path,
) -> None:
    engine = _opened_project(tmp_path)
    managed_root = tmp_path / "managed-credentials"
    assert (
        _client(engine, managed_root, deposit_names=("first",))
        .post(
            _source_paths()[0],
            json={"address": "github.com/acme/studio", "token": "first-token"},
        )
        .status_code
        == 201
    )
    conflict = PublishedProjectSourceConflict(DbosHostConfigurationChannel(engine))
    client = _client(
        engine,
        managed_root,
        deposit_names=(),
        connection_channel=conflict,
    )

    assert client.delete(_source_paths()[1]).status_code == 204
    assert client.delete(_source_paths()[1]).status_code == 204
    assert client.get(_source_paths()[0]).json() == {"items": []}
    engine.dispose()


def test_malformed_provider_json_cleans_connect_and_rotation_deposits(
    tmp_path: Path,
) -> None:
    engine = _opened_project(tmp_path)
    managed_root = tmp_path / "managed-credentials"

    def malformed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json", request=request)

    malformed_connector = GitHubProjectSourceConnector(httpx.MockTransport(malformed))
    collection, _, token_path, _ = _source_paths()
    refused_connect = _client(
        engine,
        managed_root,
        deposit_names=("invalidconnect",),
        source_connector=malformed_connector,
    ).post(
        collection,
        json={"address": "github.com/acme/studio", "token": "unpublished-token"},
    )

    assert refused_connect.status_code == 503
    assert tuple(managed_root.iterdir()) == ()

    assert (
        _client(engine, managed_root, deposit_names=("accepted",))
        .post(
            collection,
            json={"address": "github.com/acme/studio", "token": "accepted-token"},
        )
        .status_code
        == 201
    )
    latest_before_rotation = DbosHostConfigurationChannel(
        engine
    ).latest_project_source_connection_revision_by_source(PROJECT, SOURCE)
    assert isinstance(latest_before_rotation, ProjectSourceConnectionRevision)
    deposits_before_rotation = tuple(managed_root.iterdir())

    refused_rotation = _client(
        engine,
        managed_root,
        deposit_names=("invalidrotation",),
        source_connector=malformed_connector,
    ).put(token_path, json={"token": "unpublished-rotation-token"})

    assert refused_rotation.status_code == 503
    latest_after_rotation = DbosHostConfigurationChannel(
        engine
    ).latest_project_source_connection_revision_by_source(PROJECT, SOURCE)
    assert latest_after_rotation == latest_before_rotation
    assert tuple(managed_root.iterdir()) == deposits_before_rotation
    assert (
        latest_before_rotation.credential_directory / GITHUB_TOKEN_CREDENTIAL_ENTRY
    ).read_text(encoding="utf-8") == "accepted-token"
    engine.dispose()


def test_unexpected_connector_failure_cleans_the_staged_deposit(
    tmp_path: Path,
) -> None:
    engine = _opened_project(tmp_path)
    managed_root = tmp_path / "managed-credentials"

    class FailingConnector(FakeGitHubProjectSourceConnector):
        def validate(
            self,
            parsed: ParsedProjectSourceAddress,
            credential_directory: Path,
        ) -> ValidateProjectSourceResult:
            raise RuntimeError("unexpected connector failure")

    collection, _, _, _ = _source_paths()
    response = _client(
        engine,
        managed_root,
        deposit_names=("failed",),
        source_connector=FailingConnector(),
    ).post(
        collection,
        json={"address": "github.com/acme/studio", "token": "unpublished-token"},
    )

    assert response.status_code == 500
    assert tuple(managed_root.iterdir()) == ()
    engine.dispose()


def test_validation_oserror_is_fixed_unavailable_without_internal_detail(
    tmp_path: Path,
) -> None:
    engine = _opened_project(tmp_path)
    managed_root = tmp_path / "managed-credentials"

    class UnavailableConnector(FakeGitHubProjectSourceConnector):
        def validate(
            self,
            parsed: ParsedProjectSourceAddress,
            credential_directory: Path,
        ) -> ValidateProjectSourceResult:
            raise OSError("internal-path-token-canary")

    response = _client(
        engine,
        managed_root,
        deposit_names=("unavailable",),
        source_connector=UnavailableConnector(),
    ).post(
        _source_paths()[0],
        json={"address": "github.com/acme/studio", "token": "submitted-token"},
    )

    assert response.status_code == 503
    assert "internal-path-token-canary" not in response.text
    assert "submitted-token" not in response.text
    assert tuple(managed_root.iterdir()) == ()
    engine.dispose()


def test_rotate_treats_a_stored_address_refusal_as_corruption(
    tmp_path: Path,
) -> None:
    engine = _opened_project(tmp_path)
    managed_root = tmp_path / "managed-credentials"
    credential_directory = tmp_path / "legacy-credential"
    credential_directory.mkdir()
    channel = DbosHostConfigurationChannel(engine)
    channel.publish_project_source_connection_revision(
        ProjectSourceConnectionRevision(
            PROJECT,
            SOURCE,
            1,
            SourceKind("github"),
            SourceAddress("malformed-stored-address"),
            credential_directory,
            SourceConnectionAuthMethod.PERSONAL_ACCESS_TOKEN,
            ConnectionActor("legacy"),
            ProjectSourceConnectionLifecycle.CONNECTED,
            None,
            None,
        )
    )

    malformed = _client(
        engine,
        managed_root,
        deposit_names=("unused",),
        source_connector=GitHubProjectSourceConnector(),
    ).put(_source_paths()[2], json={"token": "replacement-token"})

    assert malformed.status_code == 500
    assert not managed_root.exists()
    engine.dispose()


def test_rotate_treats_validation_of_the_stored_address_as_corruption(
    tmp_path: Path,
) -> None:
    engine = _opened_project(tmp_path)
    managed_root = tmp_path / "managed-credentials"
    assert (
        _client(engine, managed_root, deposit_names=("first",))
        .post(
            _source_paths()[0],
            json={"address": "github.com/acme/studio", "token": "first-token"},
        )
        .status_code
        == 201
    )

    class RejectingStoredAddress(FakeGitHubProjectSourceConnector):
        def validate(
            self,
            parsed: ParsedProjectSourceAddress,
            credential_directory: Path,
        ) -> ValidateProjectSourceResult:
            return ProjectSourceAddressInvalid("stored provider address refused")

    refused = _client(
        engine,
        managed_root,
        deposit_names=("refused",),
        source_connector=RejectingStoredAddress(),
    ).put(_source_paths()[2], json={"token": "replacement-token"})

    assert refused.status_code == 500
    assert "stored provider address refused" not in refused.text
    assert tuple(path.name for path in managed_root.iterdir()) == (
        f"{SOURCE.value}-first",
    )
    engine.dispose()


@pytest.mark.proves("a-project-source-token-never-returns-from-its-http-door")
def test_token_validation_and_provider_refusals_never_echo_the_token(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = _opened_project(tmp_path)
    managed_root = tmp_path / "managed-credentials"
    client = _client(engine, managed_root, deposit_names=("echo",))
    collection, _, _, _ = _source_paths()
    oversized_token = "x" * (MAXIMUM_SOURCE_TOKEN_CHARACTERS + 1)

    invalid = client.post(
        collection,
        json={"address": "github.com/acme/studio", "token": oversized_token},
    )

    assert invalid.status_code == 422
    assert oversized_token not in invalid.text
    assert oversized_token not in caplog.text

    provider_token = "provider-echo-token-canary"
    refused = _client(engine, managed_root, deposit_names=("providerrefused",)).post(
        collection,
        json={"address": "github.com/acme/studio", "token": provider_token},
    )
    assert refused.status_code == 422
    assert provider_token not in refused.text
    assert provider_token not in caplog.text
    assert tuple(managed_root.iterdir()) == ()
    engine.dispose()


@pytest.mark.proves("credential-publication-follows-the-durable-source-revision")
@pytest.mark.parametrize("interruption", ["after", "raise-after"])
def test_a_write_that_landed_before_unavailability_keeps_its_deposit(
    tmp_path: Path, interruption: Literal["after", "raise-after"]
) -> None:
    engine = _opened_project(tmp_path)
    managed_root = tmp_path / "managed-credentials"
    assert (
        _client(engine, managed_root, deposit_names=("first",))
        .post(
            _source_paths()[0],
            json={"address": "github.com/acme/studio", "token": "first-token"},
        )
        .status_code
        == 201
    )
    interrupted = InterruptedProjectSourceWrites(
        DbosHostConfigurationChannel(engine), interruption, 2
    )

    rotated = _client(
        engine,
        managed_root,
        deposit_names=("rotated",),
        connection_channel=interrupted,
    ).put(_source_paths()[2], json={"token": "rotated-token"})

    assert rotated.status_code == 200
    latest = interrupted.delegate.latest_project_source_connection_revision_by_source(
        PROJECT, SOURCE
    )
    assert isinstance(latest, ProjectSourceConnectionRevision)
    assert latest.revision_number == 2
    assert latest.credential_directory.is_dir()
    assert (latest.credential_directory / GITHUB_TOKEN_CREDENTIAL_ENTRY).read_text(
        encoding="utf-8"
    ) == "rotated-token"
    engine.dispose()


@pytest.mark.proves("credential-publication-follows-the-durable-source-revision")
def test_a_connect_that_landed_before_an_exception_keeps_its_deposit(
    tmp_path: Path,
) -> None:
    engine = _opened_project(tmp_path)
    managed_root = tmp_path / "managed-credentials"
    interrupted = InterruptedProjectSourceWrites(
        DbosHostConfigurationChannel(engine), "raise-after", 1
    )

    connected = _client(
        engine,
        managed_root,
        deposit_names=("first",),
        connection_channel=interrupted,
    ).post(
        _source_paths()[0],
        json={"address": "github.com/acme/studio", "token": "first-token"},
    )

    assert connected.status_code == 201
    latest = interrupted.delegate.latest_project_source_connection_revision_by_source(
        PROJECT, SOURCE
    )
    assert isinstance(latest, ProjectSourceConnectionRevision)
    assert latest.credential_directory.is_dir()
    assert (latest.credential_directory / GITHUB_TOKEN_CREDENTIAL_ENTRY).read_text(
        encoding="utf-8"
    ) == "first-token"
    engine.dispose()


@pytest.mark.parametrize("operation", ["connect", "rotate"])
@pytest.mark.parametrize("candidate_lands", [False, True])
@pytest.mark.proves("credential-publication-follows-the-durable-source-revision")
def test_an_overtaken_write_keeps_exactly_the_deposits_durable_history_names(
    tmp_path: Path,
    operation: Literal["connect", "rotate"],
    candidate_lands: bool,
) -> None:
    engine = _opened_project(tmp_path)
    managed_root = tmp_path / "managed-credentials"
    if operation == "rotate":
        assert (
            _client(engine, managed_root, deposit_names=("initial",))
            .post(
                _source_paths()[0],
                json={"address": "github.com/acme/studio", "token": "initial-token"},
            )
            .status_code
            == 201
        )
        target_revision = 2
    else:
        target_revision = 1
    winner_directory = managed_root / f"{SOURCE.value}-winner"
    _write_credential_directory(winner_directory, "winner-token")
    overtaking = OvertakingProjectSourceWrite(
        DbosHostConfigurationChannel(engine),
        target_revision,
        candidate_lands,
        winner_directory,
    )
    client = _client(
        engine,
        managed_root,
        deposit_names=("candidate",),
        connection_channel=overtaking,
    )

    response = (
        client.post(
            _source_paths()[0],
            json={"address": "github.com/acme/studio", "token": "candidate-token"},
        )
        if operation == "connect"
        else client.put(_source_paths()[2], json={"token": "candidate-token"})
    )

    assert response.status_code == 503
    referenced = _referenced_credential_directories(engine)
    assert frozenset(managed_root.iterdir()) == referenced
    candidate_directory = managed_root / f"{SOURCE.value}-candidate"
    assert candidate_directory.exists() is candidate_lands
    assert winner_directory in referenced
    engine.dispose()


@pytest.mark.parametrize("candidate_lands", [False, True])
@pytest.mark.proves("credential-publication-follows-the-durable-source-revision")
def test_a_second_rotate_conflict_reconciles_the_candidate_deposit(
    tmp_path: Path, candidate_lands: bool
) -> None:
    engine = _opened_project(tmp_path)
    managed_root = tmp_path / "managed-credentials"
    assert (
        _client(engine, managed_root, deposit_names=("initial",))
        .post(
            _source_paths()[0],
            json={"address": "github.com/acme/studio", "token": "initial-token"},
        )
        .status_code
        == 201
    )
    winner_directory = managed_root / f"{SOURCE.value}-winner"
    _write_credential_directory(winner_directory, "winner-token")
    overtaking = SecondConflictOvertakingRotation(
        DbosHostConfigurationChannel(engine), candidate_lands, winner_directory
    )

    response = _client(
        engine,
        managed_root,
        deposit_names=("candidate",),
        connection_channel=overtaking,
    ).put(_source_paths()[2], json={"token": "candidate-token"})

    assert response.status_code == 503
    referenced = _referenced_credential_directories(engine)
    assert frozenset(managed_root.iterdir()) == referenced
    candidate_directory = managed_root / f"{SOURCE.value}-candidate"
    assert candidate_directory.exists() is candidate_lands
    engine.dispose()


@pytest.mark.proves("credential-publication-follows-the-durable-source-revision")
def test_a_write_refused_before_mutation_discards_only_the_new_deposit(
    tmp_path: Path,
) -> None:
    engine = _opened_project(tmp_path)
    managed_root = tmp_path / "managed-credentials"
    assert (
        _client(engine, managed_root, deposit_names=("first",))
        .post(
            _source_paths()[0],
            json={"address": "github.com/acme/studio", "token": "first-token"},
        )
        .status_code
        == 201
    )
    channel = DbosHostConfigurationChannel(engine)
    latest_before = channel.latest_project_source_connection_revision_by_source(
        PROJECT, SOURCE
    )
    assert isinstance(latest_before, ProjectSourceConnectionRevision)

    refused = _client(
        engine,
        managed_root,
        deposit_names=("refused",),
        connection_channel=InterruptedProjectSourceWrites(channel, "before", 2),
    ).put(_source_paths()[2], json={"token": "refused-rotation-token"})

    assert refused.status_code == 503
    latest_after = channel.latest_project_source_connection_revision_by_source(
        PROJECT, SOURCE
    )
    assert latest_after == latest_before
    assert (
        latest_before.credential_directory / GITHUB_TOKEN_CREDENTIAL_ENTRY
    ).read_text(encoding="utf-8") == "first-token"
    assert tuple(path.name for path in managed_root.iterdir()) == (
        f"{SOURCE.value}-first",
    )
    engine.dispose()


@pytest.mark.proves("credential-publication-follows-the-durable-source-revision")
def test_rotate_rereads_and_retries_after_a_revision_conflict(tmp_path: Path) -> None:
    engine = _opened_project(tmp_path)
    managed_root = tmp_path / "managed-credentials"
    assert (
        _client(engine, managed_root, deposit_names=("first",))
        .post(
            _source_paths()[0],
            json={"address": "github.com/acme/studio", "token": "first-token"},
        )
        .status_code
        == 201
    )
    conflict = ConflictingProjectSourceRotation(DbosHostConfigurationChannel(engine))

    rotated = _client(
        engine,
        managed_root,
        deposit_names=("rotated",),
        connection_channel=conflict,
    ).put(_source_paths()[2], json={"token": "rotated-token"})

    assert rotated.status_code == 200
    assert rotated.json()["revision"] == 3
    assert rotated.json()["public_source_reference"] == _source_paths()[3]
    assert rotated.json()["connected_at"] == FIRST_CONNECTED_AT.value
    latest = conflict.delegate.latest_project_source_connection_revision_by_source(
        PROJECT, SOURCE
    )
    assert isinstance(latest, ProjectSourceConnectionRevision)
    assert latest.revision_number == 3
    assert latest.source_ref == SourceReference("main")
    assert (latest.credential_directory / GITHUB_TOKEN_CREDENTIAL_ENTRY).read_text(
        encoding="utf-8"
    ) == "rotated-token"
    engine.dispose()


@pytest.mark.proves("a-project-source-identity-never-includes-a-branch")
@pytest.mark.proves("credential-publication-follows-the-durable-source-revision")
def test_concurrent_connects_publish_one_source_and_clean_the_loser_deposit(
    tmp_path: Path,
) -> None:
    first_engine = _opened_project(tmp_path)
    second_engine = opened_channel(tmp_path)
    managed_root = tmp_path / "managed-credentials"
    barrier = Barrier(2)

    class RacingConnector(FakeGitHubProjectSourceConnector):
        def validate(
            self,
            parsed: ParsedProjectSourceAddress,
            credential_directory: Path,
        ) -> ValidateProjectSourceResult:
            barrier.wait()
            return super().validate(parsed, credential_directory)

    candidates = (
        (
            first_engine,
            ProjectSourceId("11111111-1111-4111-8111-111111111111"),
            "first-token",
            "first",
        ),
        (
            second_engine,
            ProjectSourceId("22222222-2222-4222-8222-222222222222"),
            "second-token",
            "second",
        ),
    )

    def connect(
        candidate: tuple[Engine, ProjectSourceId, str, str],
    ) -> ManagedProjectSourcePublished | ProjectSourceAlreadyConnected:
        engine, source_id, token, deposit_name = candidate
        channel = DbosHostConfigurationChannel(engine)
        result = connect_managed_project_source(
            PROJECT,
            PROJECT,
            "github.com/acme/studio",
            token,
            channel,
            channel,
            RacingConnector(),
            FilesystemProjectSourceCredentialStore(
                managed_root, deposit_name=lambda: deposit_name
            ),
            source_id_generator=lambda: source_id,
            clock=lambda: FIRST_CONNECTED_AT,
        )
        assert isinstance(
            result, (ManagedProjectSourcePublished, ProjectSourceAlreadyConnected)
        )
        return result

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(connect, candidates))

    (winner,) = tuple(
        result
        for result in results
        if isinstance(result, ManagedProjectSourcePublished)
    )
    (loser,) = tuple(
        result
        for result in results
        if isinstance(result, ProjectSourceAlreadyConnected)
    )
    assert loser.source_id == winner.source.source_id
    latest = DbosHostConfigurationChannel(
        first_engine
    ).latest_project_source_connection_revisions(PROJECT)
    assert isinstance(latest, tuple)
    (active,) = latest
    assert active.lifecycle is ProjectSourceConnectionLifecycle.CONNECTED
    assert active.source_id == winner.source.source_id
    (published_deposit,) = tuple(managed_root.iterdir())
    assert published_deposit.name.startswith(winner.source.source_id.value)
    winning_token = {candidate[1]: candidate[2] for candidate in candidates}[
        winner.source.source_id
    ]
    assert (published_deposit / GITHUB_TOKEN_CREDENTIAL_ENTRY).read_text(
        encoding="utf-8"
    ) == winning_token
    first_engine.dispose()
    second_engine.dispose()
