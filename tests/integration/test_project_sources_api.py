"""The HTTP project-source doors against the real durable store."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import httpx
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
    ProjectId,
    ProjectSourceConnectionLifecycle,
    ProjectSourceConnectionRevision,
    ProjectSourceId,
    SourceAddress,
)
from atelier2.contracts.when import RecordedAt
from atelier2.ports.project_connections import (
    ParsedProjectSourceAddress,
    ParseProjectSourceAddressResult,
    ProjectSourceAddressInvalid,
    ProjectSourceAuthenticationRefused,
    ProjectSourceConnector,
    ProjectSourceValidationUnavailable,
    ValidatedProjectSource,
    ValidateProjectSourceResult,
)
from tests.integration.test_host_configuration import opened_channel
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
        if token == "unavailable-token":
            return ProjectSourceValidationUnavailable("provider network unavailable")
        return ValidatedProjectSource(
            parsed.source_kind,
            SourceAddress(f"{parsed.public_address}@main"),
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
        if not separator or not branch:
            raise ValueError("the stored source address has no branch")
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
) -> TestClient:
    channel = DbosHostConfigurationChannel(engine)
    return TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=api_ports(
                host_configuration_channel=channel,
                project_source_connection_channel=channel,
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
    assert source_reference in duplicate.json()["detail"]
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
    assert (latest.credential_directory / GITHUB_TOKEN_CREDENTIAL_ENTRY).read_text(
        encoding="utf-8"
    ) == "rotated-token"

    engine.dispose()
    with sqlite3.connect(tmp_path / "atelier.sqlite") as connection:
        durable_bytes = "\n".join(connection.iterdump())
    for token in ("first-token", "second-token", "reconnect-token", "rotated-token"):
        assert token not in durable_bytes


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
    assert refused.json()["detail"] == "Bad credentials from the fake provider"
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

    assert response.status_code == 503
    assert tuple(managed_root.iterdir()) == ()
    engine.dispose()


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
