"""A start that names no binding runs on what the served project has cast.

The live defect (`#680`): a workflow whose roles the operator had occupied in
the console could not be started by the conductor, because the occupancy was
read only by the manual start page. These tests drive the real HTTP door with
the exact body the conductor's `start_run` builds, against a real store whose
project holds a real occupancy -- so what they prove is the run's own durable
binding matrix, not a call made somewhere.

The queue's launch sweep is here too, rather than beside the queue's other
launch proofs: what it demonstrates is this seam -- an item nobody bound by
hand starts because the project had cast its roles -- and it demonstrates it
with the same occupancy the conductor's start uses.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.host_configuration import DbosHostConfigurationChannel
from atelier2.adapters.dbos.queue_projection_store import DbosQueueProjectionStore
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import run_agent_bindings, runs
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.api.openapi import API_PREFIX
from atelier2.contracts.agents import (
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentConfigurationRevisionHash,
    AgentExecutionCapability,
    AgentExecutorRevision,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.contracts.catalog_v3 import (
    CatalogActivatedAt,
    CatalogActor,
    CatalogLineageDisplayName,
    CatalogLineageId,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.host_configuration import (
    OccupancyBinding,
    OccupancyRevision,
    ProjectId,
)
from atelier2.contracts.queue_projection import (
    AdmitQueueItem,
    QueueAdmission,
    QueueAdmissionRationale,
    QueueItemAdmitted,
    QueueProjectionRevision,
    TrackerItemReference,
    WorkItemReference,
)
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.runs import RunState, WorkflowRevision
from atelier2.host.run_command import AgentRoleBinding, start_request_body
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AuthProfileRevisionCreated,
    AuthProfileRevisionExisting,
)
from atelier2.ports.host_configuration import OccupancyRevisionCreated
from atelier2.ports.published_revisions import CatalogLineageFounded
from tests.scenarios.agents import RecordingAgentExecutorFactoryV2, agent_scratch_root
from tests.scenarios.api import durable_api_client
from tests.scenarios.projects import declaring_verification, git_project
from tests.scenarios.runs import publish_revision

SERVED_PROJECT = ProjectId("atelier")
JSON_MEDIA_TYPE = "application/json"

ONE_ROLE_DOCUMENT = b"""format_version: 2
start: implement
nodes:
  - {id: done, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: implement, type: agent, role: builder, job: build, next: done}
"""

TWO_ROLE_DOCUMENT = b"""format_version: 2
start: implement
nodes:
  - {id: done, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: review, type: agent, role: reviewer, job: review, next: done}
  - {id: implement, type: agent, role: builder, job: build, next: review}
"""


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    project_root = tmp_path / "project"
    git_project(project_root, declaring_verification(["true"]))
    started = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "occupancy-cast-test",
            agent_scratch_root=agent_scratch_root(tmp_path),
            project_id=SERVED_PROJECT,
            bootstrap_project_root=project_root,
        ),
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        ExactOutputAgentExecutorFactory(),
        (
            RecordingAgentExecutorFactoryV2(
                "exact", "exact/v1", "exact-operation", b'"the exact bytes"'
            ),
        ),
    )
    started.initialize_storage()
    try:
        yield started
    finally:
        started.close()


def publish_workflow(
    runtime: DbosRuntime, document: bytes = ONE_ROLE_DOCUMENT
) -> tuple[WorkflowRevision, CatalogLineageId]:
    """Publish one document and take the catalog name an occupancy is keyed by."""
    revision = WorkflowRevision(document)
    publish_revision(runtime.engine, revision)
    catalog = DbosCatalogStore(runtime.engine)
    published = PublishedRevision(RevisionKind.WORKFLOW, document)
    catalog.publish_revision(published)
    founded = catalog.found_lineage(
        published,
        CatalogLineageDisplayName("occupied-workflow"),
        CatalogActor("operator"),
        CatalogActivatedAt("2026-08-25T09:00:00Z"),
    )
    assert isinstance(founded, CatalogLineageFounded), founded
    return revision, founded.lineage.lineage_id


def publish_configuration(
    runtime: DbosRuntime, model: str
) -> AgentConfigurationRevisionHash:
    """One published agent configuration an occupancy or a caller can name."""
    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    auth = AuthProfileRevision("max", 1, ProviderId("exact"), AuthMode.SUBSCRIPTION)
    assert isinstance(
        catalog.publish_auth_profile_revision(auth),
        (AuthProfileRevisionCreated, AuthProfileRevisionExisting),
    )
    configuration = AgentConfigurationRevision(
        model,
        auth.revision_hash,
        AgentExecutorRevision("exact/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    assert isinstance(
        catalog.publish_agent_configuration_revision(configuration),
        AgentConfigurationRevisionCreated,
    )
    return configuration.revision_hash


def occupy(
    runtime: DbosRuntime,
    lineage_id: CatalogLineageId,
    bindings: tuple[tuple[str, AgentConfigurationRevisionHash], ...],
    revision_number: int = 1,
) -> None:
    """Cast these roles on this workflow, as the console's occupancy does."""
    published = DbosHostConfigurationChannel(runtime.engine).publish_occupancy_revision(
        OccupancyRevision(
            SERVED_PROJECT,
            lineage_id,
            revision_number,
            tuple(
                OccupancyBinding(AgentRole(role), configuration)
                for role, configuration in bindings
            ),
        )
    )
    assert isinstance(published, OccupancyRevisionCreated), published


def admit_queue_item(runtime: DbosRuntime, lineage_id: CatalogLineageId) -> None:
    """Bind one tracker item to this workflow and admit it, as the queue does."""
    admitted = DbosQueueProjectionStore(runtime.engine).admit(
        AdmitQueueItem(
            WorkItemReference(SERVED_PROJECT, TrackerItemReference("gh:680")),
            QueueAdmission(
                lineage_id, QueueAdmissionRationale("the triage rule matched")
            ),
            QueueProjectionRevision(0),
        )
    )
    assert isinstance(admitted, QueueItemAdmitted), admitted


def conductor_start(
    client: TestClient,
    run_id: str,
    revision: WorkflowRevision,
    bindings: tuple[AgentRoleBinding, ...] = (),
) -> Response:
    """POST /runs with the exact body the conductor's `start_run` tool builds."""
    return client.post(
        API_PREFIX + "/runs",
        content=start_request_body(run_id, revision.revision_hash.value, bindings),
        headers={"content-type": JSON_MEDIA_TYPE},
    )


def stored_bindings(engine: Engine, run_id: str) -> list[tuple[str, str]]:
    """The role matrix the run itself carries, in the store's own words."""
    with engine.connect() as connection:
        return [
            (str(record["role"]), str(record["agent_configuration_revision_hash"]))
            for record in connection.execute(
                sa.select(run_agent_bindings)
                .where(run_agent_bindings.c.run_id == run_id)
                .order_by(run_agent_bindings.c.role)
            ).mappings()
        ]


def run_ids(engine: Engine) -> list[str]:
    """Every run this store holds, however it was started."""
    with engine.connect() as connection:
        return [
            str(value)
            for value in connection.execute(sa.select(runs.c.run_id)).scalars()
        ]


def test_a_start_without_bindings_runs_on_the_projects_occupancy(
    runtime: DbosRuntime,
) -> None:
    revision, lineage_id = publish_workflow(runtime)
    occupied = publish_configuration(runtime, "opus")
    occupy(runtime, lineage_id, (("builder", occupied),))
    client = durable_api_client(runtime, served_project_id=SERVED_PROJECT)

    started = conductor_start(client, "conductor/occupied", revision)

    assert started.status_code == 201, started.text
    assert started.json()["state"] == RunState.STARTED.value
    assert stored_bindings(runtime.engine, "conductor/occupied") == [
        ("builder", occupied.value)
    ]


def test_an_explicit_binding_wins_over_the_projects_occupancy(
    runtime: DbosRuntime,
) -> None:
    revision, lineage_id = publish_workflow(runtime)
    occupied = publish_configuration(runtime, "opus")
    named = publish_configuration(runtime, "sonnet")
    occupy(runtime, lineage_id, (("builder", occupied),))
    client = durable_api_client(runtime, served_project_id=SERVED_PROJECT)

    started = conductor_start(
        client,
        "conductor/explicit",
        revision,
        (AgentRoleBinding("builder", named.value),),
    )

    assert started.status_code == 201, started.text
    assert stored_bindings(runtime.engine, "conductor/explicit") == [
        ("builder", named.value)
    ]


def test_a_role_nobody_occupied_is_refused_exactly_as_before(
    runtime: DbosRuntime,
) -> None:
    revision, lineage_id = publish_workflow(runtime, TWO_ROLE_DOCUMENT)
    occupied = publish_configuration(runtime, "opus")
    occupy(runtime, lineage_id, (("builder", occupied),))
    client = durable_api_client(runtime, served_project_id=SERVED_PROJECT)

    refused = conductor_start(client, "conductor/half-occupied", revision)

    assert refused.json()["type"].endswith("invalid-agent-bindings"), refused.text
    assert run_ids(runtime.engine) == []


def test_an_admitted_queue_item_starts_on_the_projects_occupancy(
    runtime: DbosRuntime,
) -> None:
    """The launch sweep names no binding, so until now it could only wait."""
    _revision, lineage_id = publish_workflow(runtime)
    occupied = publish_configuration(runtime, "opus")
    occupy(runtime, lineage_id, (("builder", occupied),))
    admit_queue_item(runtime, lineage_id)

    runtime.launch()

    started = run_ids(runtime.engine)
    assert len(started) == 1
    assert stored_bindings(runtime.engine, started[0]) == [("builder", occupied.value)]


def test_retrying_a_run_after_the_occupancy_changed_conflicts(
    runtime: DbosRuntime,
) -> None:
    """The run keeps the matrix it started with, so a moved occupancy says so.

    A cast start pins the binding-set hash the occupancy produced. Re-deriving
    the same run id under a different occupancy is therefore a different run
    asking for an identity that is taken -- which is what the conflict is for,
    rather than answering with a run whose bindings the caller no longer means.
    """
    revision, lineage_id = publish_workflow(runtime)
    first = publish_configuration(runtime, "opus")
    recast = publish_configuration(runtime, "sonnet")
    occupy(runtime, lineage_id, (("builder", first),))
    client = durable_api_client(runtime, served_project_id=SERVED_PROJECT)
    assert conductor_start(client, "conductor/recast", revision).status_code == 201

    occupy(runtime, lineage_id, (("builder", recast),), revision_number=2)
    retried = conductor_start(client, "conductor/recast", revision)

    assert retried.json()["type"].endswith("run-identity-conflict"), retried.text
    assert stored_bindings(runtime.engine, "conductor/recast") == [
        ("builder", first.value)
    ]
