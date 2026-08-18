"""A project exists durably and a run can optionally start under one (#133 Kopf 1).

These drive the real HTTP door against a real durable store: create a project,
read it back by id and by list, refuse a duplicate name, start a run naming a
project (a fake executor that is registered but never opened -- every test
here only starts a run, leaving it STARTED at its entry agent node) and see
the list filter by it, and refuse an unknown project reference before any
row exists.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import runs as runs_table
from atelier2.adapters.dbos.starter import DbosDurableRunStarter
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.api.openapi import API_PREFIX
from atelier2.api.references import encode_public_run_reference
from atelier2.application.start_published_run import (
    AuthoredAgentBinding,
    ProjectUnknown,
    RunCreated,
)
from atelier2.application.start_published_run import (
    start_published_run as start_published_run_use_case,
)
from atelier2.contracts.agents import (
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutorRevision,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.projects import ProjectId
from atelier2.contracts.runs import RunId, WorkflowRevision
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AuthProfileRevisionCreated,
)
from tests.scenarios.agents import RecordingAgentExecutorFactoryV2, agent_scratch_root
from tests.scenarios.api import durable_api_client
from tests.scenarios.runs import publish_revision

PROJECTS_PATH = API_PREFIX + "/projects"
RUNS_PATH = API_PREFIX + "/runs"

V2_DOCUMENT = b"""format_version: 2
start: build
nodes:
  - {id: done, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: build, type: agent, role: builder, job: build, next: done}
"""


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    """A runtime whose one registered V2 executor is never opened.

    Every test here only starts a run, leaving it STARTED at its `builder`
    entry node; the factory below satisfies the binding checks a start makes
    and is reached by no attempt.
    """

    started = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "project-lifecycle-test",
            agent_scratch_root=agent_scratch_root(tmp_path),
        ),
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        ExactOutputAgentExecutorFactory(),
        (
            RecordingAgentExecutorFactoryV2(
                "anthropic", "claude-cli/v1", "op-claude", b""
            ),
        ),
    )
    started.initialize_storage()
    try:
        yield started
    finally:
        started.close()


def client(runtime: DbosRuntime) -> TestClient:
    return durable_api_client(runtime)


def publish_builder_configuration(runtime: DbosRuntime) -> AgentConfigurationRevision:
    """The one agent role `V2_DOCUMENT` binds, published once per test."""

    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    auth = AuthProfileRevision("max", 1, ProviderId("anthropic"), AuthMode.SUBSCRIPTION)
    assert isinstance(
        catalog.publish_auth_profile_revision(auth), AuthProfileRevisionCreated
    )
    configuration = AgentConfigurationRevision(
        "claude-opus-4-1",
        auth.revision_hash,
        AgentExecutorRevision("claude-cli/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    assert isinstance(
        catalog.publish_agent_configuration_revision(configuration),
        AgentConfigurationRevisionCreated,
    )
    return configuration


def test_a_project_is_created_and_read_back_by_id_and_by_list(
    runtime: DbosRuntime,
) -> None:
    api = client(runtime)

    created = api.post(PROJECTS_PATH, json={"name": "employer-repo"})

    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "employer-repo"
    project_id = body["project_id"]

    fetched = api.get(f"{PROJECTS_PATH}/{project_id}")
    assert fetched.status_code == 200
    assert fetched.json() == body

    listed = api.get(PROJECTS_PATH)
    assert listed.status_code == 200
    assert listed.json() == {"items": [body], "next_after_project_id": None}


def test_a_project_that_was_never_created_is_refused_by_name(
    runtime: DbosRuntime,
) -> None:
    response = client(runtime).get(f"{PROJECTS_PATH}/{'aa' * 32}")

    assert response.status_code == 404
    assert response.json()["type"].endswith("project-unknown")


def test_a_duplicate_project_name_is_refused_and_creates_no_second_row(
    runtime: DbosRuntime,
) -> None:
    api = client(runtime)
    first = api.post(PROJECTS_PATH, json={"name": "shared-name"})
    assert first.status_code == 201

    second = api.post(PROJECTS_PATH, json={"name": "shared-name"})

    assert second.status_code == 409
    assert second.json()["type"].endswith("project-name-collision")
    listed = api.get(PROJECTS_PATH)
    assert [item["project_id"] for item in listed.json()["items"]] == [
        first.json()["project_id"]
    ]


def test_an_unknown_project_reference_is_refused_before_any_run_row(
    runtime: DbosRuntime,
) -> None:
    api = client(runtime)
    revision = WorkflowRevision(V2_DOCUMENT)
    publish_revision(runtime.engine, revision)
    configuration = publish_builder_configuration(runtime)
    never_minted = "ab" * 32

    response = api.post(
        RUNS_PATH,
        json={
            "workflow_format_version": 2,
            "run_id": "project-unknown-run",
            "workflow_revision_hash": revision.revision_hash.value,
            "agent_bindings": [
                {
                    "role": "builder",
                    "agent_configuration_revision_hash": (
                        configuration.revision_hash.value
                    ),
                }
            ],
            "project_id": never_minted,
        },
    )

    assert response.status_code == 404
    assert response.json()["type"].endswith("project-unknown")
    with runtime.engine.connect() as connection:
        count = connection.scalar(
            sa.select(sa.func.count())
            .select_from(runs_table)
            .where(runs_table.c.run_id == "project-unknown-run")
        )
    assert count == 0


def test_a_run_starts_under_a_project_and_the_list_filters_by_it(
    runtime: DbosRuntime,
) -> None:
    api = client(runtime)
    created_project = api.post(PROJECTS_PATH, json={"name": "the-one-project"})
    project_id = created_project.json()["project_id"]
    revision = WorkflowRevision(V2_DOCUMENT)
    publish_revision(runtime.engine, revision)
    configuration = publish_builder_configuration(runtime)

    started = api.post(
        RUNS_PATH,
        json={
            "workflow_format_version": 2,
            "run_id": "project-bound-run",
            "workflow_revision_hash": revision.revision_hash.value,
            "agent_bindings": [
                {
                    "role": "builder",
                    "agent_configuration_revision_hash": (
                        configuration.revision_hash.value
                    ),
                }
            ],
            "project_id": project_id,
        },
    )

    assert started.status_code == 201, started.text
    assert started.json()["project_id"] == project_id

    read_back = api.get(f"{RUNS_PATH}/{started.json()['public_run_reference']}")
    assert read_back.json()["project_id"] == project_id

    filtered = api.get(RUNS_PATH, params={"project": project_id})
    assert [item["run_id"] for item in filtered.json()["items"]] == [
        "project-bound-run"
    ]

    other_project = api.post(PROJECTS_PATH, json={"name": "a-different-project"})
    excluded = api.get(
        RUNS_PATH, params={"project": other_project.json()["project_id"]}
    )
    assert excluded.json()["items"] == []


def test_a_projectless_run_start_reads_back_with_no_project(
    runtime: DbosRuntime,
) -> None:
    """The default workshop experience: no project named, nothing about it changes."""

    revision = WorkflowRevision(V2_DOCUMENT)
    publish_revision(runtime.engine, revision)
    configuration = publish_builder_configuration(runtime)
    starter = DbosDurableRunStarter(
        runtime.engine, runtime.settings, runtime.agent_executor_registry
    )
    run_id = RunId("v2/without-project")

    result = start_published_run_use_case(
        run_id,
        revision.revision_hash,
        (AuthoredAgentBinding("builder", configuration.revision_hash.value),),
        starter,
        project_id=None,
    )

    assert isinstance(result, RunCreated)
    api = client(runtime)
    read_back = api.get(f"{RUNS_PATH}/{encode_public_run_reference(run_id)}")
    assert read_back.json()["project_id"] is None


def test_project_unknown_is_named_at_the_application_layer_too(
    runtime: DbosRuntime,
) -> None:
    """The same refusal the fake-executor start proves, one layer down."""

    revision = WorkflowRevision(V2_DOCUMENT)
    publish_revision(runtime.engine, revision)
    configuration = publish_builder_configuration(runtime)
    starter = DbosDurableRunStarter(
        runtime.engine, runtime.settings, runtime.agent_executor_registry
    )

    result = start_published_run_use_case(
        RunId("v2/unknown-project"),
        revision.revision_hash,
        (AuthoredAgentBinding("builder", configuration.revision_hash.value),),
        starter,
        project_id=ProjectId("cd" * 32),
    )

    assert isinstance(result, ProjectUnknown)
