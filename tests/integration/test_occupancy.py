"""Recommended occupancy is project configuration on the wire."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from atelier2.adapters.dbos.host_configuration import (
    DbosHostConfigurationChannel,
    append_project_root,
)
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import host_occupancy_revisions
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.api.openapi import API_PREFIX, OCCUPANCY_PATH
from atelier2.api.references import encode_public_project_reference
from atelier2.contracts.agents import AgentConfigurationRevisionHash, AgentRole
from atelier2.contracts.catalog_v3 import CatalogLineageId
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.host_configuration import (
    OccupancyBinding,
    OccupancyRevision,
    ProjectId,
)
from atelier2.ports.host_configuration import (
    OccupancyRevisionCollision,
    OccupancyRevisionCreated,
)
from tests.scenarios.api import durable_api_client
from tests.scenarios.runtime import exact_output_runtime

LINEAGE = "ab" * 32
CONFIGURATION = "cd" * 32
OTHER_CONFIGURATION = "ee" * 32
PROJECT = "studio"


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    started = exact_output_runtime(
        DbosRuntimeSettings(tmp_path / "atelier.sqlite", "occupancy-door-test"),
        LoopbackEffectAdapterFactory(
            tmp_path / "effects.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("occupancy-door-test"),
        ),
    )
    try:
        yield started
    finally:
        started.close()


def _client(runtime: DbosRuntime, tmp_path: Path, project: str = PROJECT) -> TestClient:
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    append_project_root(runtime.engine, ProjectId(project), root)
    return durable_api_client(runtime)


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


def _expected() -> OccupancyRevision:
    return OccupancyRevision(
        ProjectId(PROJECT),
        CatalogLineageId(LINEAGE),
        1,
        (
            OccupancyBinding(
                AgentRole("chef"), AgentConfigurationRevisionHash(CONFIGURATION)
            ),
        ),
    )


@pytest.mark.proves("recommended-occupancy-is-project-configuration-on-the-wire")
def test_put_then_get_round_trips_occupancy_for_a_configured_project(
    runtime: DbosRuntime, tmp_path: Path
) -> None:
    client = _client(runtime, tmp_path)
    expected = _expected()

    written = client.put(_path(), json=_body())
    read = client.get(_path())

    assert written.status_code == 201
    assert read.status_code == 200
    assert written.json() == read.json()
    assert written.json()["occupancy_revision_hash"] == expected.revision_hash.value
    assert written.json()["bindings"] == [
        {
            "role": "chef",
            "agent_configuration_revision_hash": CONFIGURATION,
        }
    ]


@pytest.mark.proves("recommended-occupancy-is-project-configuration-on-the-wire")
def test_a_configured_project_without_occupancy_is_occupancy_missing(
    runtime: DbosRuntime, tmp_path: Path
) -> None:
    client = _client(runtime, tmp_path)

    response = client.get(_path())

    assert response.status_code == 404
    assert response.json()["type"].endswith("occupancy-missing")


@pytest.mark.proves("recommended-occupancy-is-project-configuration-on-the-wire")
def test_an_unknown_project_is_project_unknown(
    runtime: DbosRuntime, tmp_path: Path
) -> None:
    client = _client(runtime, tmp_path)

    response = client.get(_path(project="unknown"))

    assert response.status_code == 404
    assert response.json()["type"].endswith("project-unknown")


def test_the_same_occupancy_put_is_idempotent(
    runtime: DbosRuntime, tmp_path: Path
) -> None:
    client = _client(runtime, tmp_path)

    first = client.put(_path(), json=_body())
    second = client.put(_path(), json=_body())

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json() == second.json()


def test_a_different_occupancy_at_the_same_revision_conflicts(
    runtime: DbosRuntime, tmp_path: Path
) -> None:
    client = _client(runtime, tmp_path)
    client.put(_path(), json=_body())

    response = client.put(_path(), json=_body(configuration=OTHER_CONFIGURATION))

    assert response.status_code == 409
    assert response.json()["type"].endswith("occupancy-revision-conflict")


def test_a_slash_bearing_configured_project_is_addressable(
    runtime: DbosRuntime, tmp_path: Path
) -> None:
    project = "team/red"
    client = _client(runtime, tmp_path, project=project)

    written = client.put(_path(project=project), json=_body())
    read = client.get(_path(project=project))
    raw = client.get(f"{API_PREFIX}/projects/{project}/occupancy/{LINEAGE}")

    assert written.status_code == 201
    assert read.status_code == 200
    assert written.json()["project_id"] == project
    assert written.json() == read.json()
    assert raw.status_code == 404
    assert raw.json()["type"].endswith("route-not-found")


def test_one_hundred_bindings_persist_and_one_hundred_one_do_not(
    runtime: DbosRuntime, tmp_path: Path
) -> None:
    client = _client(runtime, tmp_path)
    hundred = [
        {
            "role": f"role{index}",
            "agent_configuration_revision_hash": CONFIGURATION,
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
                    "agent_configuration_revision_hash": CONFIGURATION,
                },
            ],
        },
    )
    with runtime.engine.connect() as connection:
        stored = connection.execute(
            sa.select(sa.func.count()).select_from(host_occupancy_revisions)
        ).scalar_one()

    assert accepted.status_code == 201
    assert client.get(_path()).json() == accepted.json()
    assert len(accepted.json()["bindings"]) == 100
    assert refused.status_code == 422
    assert refused.json()["type"].endswith("invalid-request")
    assert stored == 1


def test_concurrent_equal_puts_write_one_occupancy_row(
    runtime: DbosRuntime, tmp_path: Path
) -> None:
    client = _client(runtime, tmp_path)
    barrier = Barrier(2)

    def put(_worker: int):
        barrier.wait(timeout=5)
        return client.put(_path(), json=_body())

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(put, (0, 1)))

    assert sorted(response.status_code for response in results) == [200, 201]
    assert results[0].json() == results[1].json()
    with runtime.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(host_occupancy_revisions)
            ).scalar_one()
            == 1
        )


def test_concurrent_opposing_puts_keep_one_winner(
    runtime: DbosRuntime, tmp_path: Path
) -> None:
    client = _client(runtime, tmp_path)
    barrier = Barrier(2)

    def put(configuration: str):
        barrier.wait(timeout=5)
        return client.put(_path(), json=_body(configuration=configuration))

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(put, CONFIGURATION)
        second = pool.submit(put, OTHER_CONFIGURATION)
        results = [first.result(), second.result()]

    statuses = sorted(response.status_code for response in results)
    winners = [response for response in results if response.status_code == 201]
    losers = [response for response in results if response.status_code == 409]
    assert statuses == [201, 409]
    assert len(winners) == 1
    assert losers[0].json()["type"].endswith("occupancy-revision-conflict")
    assert client.get(_path()).json() == winners[0].json()
    with runtime.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(host_occupancy_revisions)
            ).scalar_one()
            == 1
        )


def test_tampered_occupancy_bytes_are_durable_state_corrupt(
    runtime: DbosRuntime, tmp_path: Path
) -> None:
    client = _client(runtime, tmp_path)
    assert client.put(_path(), json=_body()).status_code == 201
    with runtime.engine.connect() as connection:
        connection.execute(sa.text("DROP TRIGGER host_occupancy_bindings_no_update"))
        connection.execute(
            sa.text("UPDATE host_occupancy_bindings SET role = 'tampered'")
        )
        connection.commit()

    reading = client.get(_path())
    writing = client.put(_path(), json=_body())

    assert reading.status_code == 500
    assert reading.json()["type"].endswith("durable-state-corrupt")
    assert writing.status_code == 500
    assert writing.json()["type"].endswith("durable-state-corrupt")


def test_a_same_hash_with_different_fields_is_occupancy_revision_collision(
    runtime: DbosRuntime, tmp_path: Path
) -> None:
    first = OccupancyRevision(
        ProjectId(PROJECT),
        CatalogLineageId(LINEAGE),
        1,
        (
            OccupancyBinding(
                AgentRole("chef"), AgentConfigurationRevisionHash(CONFIGURATION)
            ),
        ),
    )
    colliding = OccupancyRevision(
        ProjectId("other"),
        CatalogLineageId(LINEAGE),
        1,
        (
            OccupancyBinding(
                AgentRole("chef"),
                AgentConfigurationRevisionHash(OTHER_CONFIGURATION),
            ),
        ),
    )
    object.__setattr__(colliding, "revision_hash", first.revision_hash)
    channel = DbosHostConfigurationChannel(runtime.engine)
    created = channel.publish_occupancy_revision(first)

    result = channel.publish_occupancy_revision(colliding)

    assert created == OccupancyRevisionCreated(first)
    assert result == OccupancyRevisionCollision()


def test_a_malformed_lineage_id_is_catalog_lineage_missing(
    runtime: DbosRuntime, tmp_path: Path
) -> None:
    client = _client(runtime, tmp_path)

    response = client.get(_path(lineage="not-a-lineage"))

    assert response.status_code == 404
    assert response.json()["type"].endswith("catalog-lineage-missing")
