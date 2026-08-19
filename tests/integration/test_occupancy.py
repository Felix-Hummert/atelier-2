"""Recommended occupancy is project configuration on the wire."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atelier2.adapters.dbos.host_configuration import append_project_root
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.api.openapi import OCCUPANCY_PATH
from atelier2.contracts.agents import AgentConfigurationRevisionHash, AgentRole
from atelier2.contracts.catalog_v3 import CatalogLineageId
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.host_configuration import (
    OccupancyBinding,
    OccupancyRevision,
    ProjectId,
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


def _client(runtime: DbosRuntime, tmp_path: Path) -> TestClient:
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    append_project_root(runtime.engine, ProjectId(PROJECT), root)
    return durable_api_client(runtime)


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
