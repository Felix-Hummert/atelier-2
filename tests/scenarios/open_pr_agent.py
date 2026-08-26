"""One V3 agent node that opens its own pull request through a declared grant.

The integration slice and the crash twin both drive the same shape: a single
headless agent node whose only output is the pull-request spec, pinning an
`open-pr` tool grant. The publish, the executor identity, and the request bytes
live here so the two drivers never disagree about which provider the run binds
or which bytes the pull request must carry -- a mismatch there would fail the
run for a reason unrelated to what either test asks.
"""

from __future__ import annotations

import json

import sqlalchemy as sa

from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.dbos.schema import agent_attempts, runs
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.dbos.workflow_ids import driving_workflow_ids
from atelier2.contracts.agent_attempts import (
    AgentAttempt,
    AgentAttemptId,
    AgentAttemptState,
)
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutionRequestHash,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.runs import (
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.contracts.tool_grants_v3 import ToolGrantCapability
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AuthProfileRevisionCreated,
)
from atelier2.ports.durable_runs import DurableRunCreated, StartPublishedRunRequestV2
from atelier2.ports.published_revisions import (
    PublishedRevisionCreated,
    PublishedRevisionExisting,
)
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    publish_checked_model_registry,
)
from tests.scenarios.workflows import ANY_JSON_SCHEMA, declared_output

PROVIDER = "exact"
"""The provider id both the published configuration and the wired executor name."""
EXECUTOR_REVISION = "exact/v1"
OPERATIONAL_IDENTITY = "exact-op"
BUILDER_ROLE = "builder"

PR_SPEC = json.dumps(
    {"title": "Ship the open-pr slice", "opened_by": "the agent itself"}
).encode("utf-8")
"""The one output the agent emits: the pull request its grant opens."""

OPEN_PR_GRANT = json.dumps({"capability": ToolGrantCapability.OPEN_PR.value}).encode(
    "utf-8"
)


def open_pr_agent_executor_factory(output: bytes) -> RecordingAgentExecutorFactoryV2:
    """The headless executor whose output is the pull-request spec the grant opens."""
    return RecordingAgentExecutorFactoryV2(
        PROVIDER, EXECUTOR_REVISION, OPERATIONAL_IDENTITY, output
    )


def _grant_document(tools_line: str) -> bytes:
    return (
        b"""format_version: 3
name: One agent that opens its own pull request
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Draft the pull request this chain opens.
"""
        + tools_line.encode("ascii")
        + declared_output()
    )


def _granted_tools_line() -> str:
    revision = PublishedRevision(RevisionKind.TOOL, OPEN_PR_GRANT).revision_hash.value
    return f"    tools:\n      - {{ref: open-pr, revision: {revision}}}\n"


def publish_open_pr_agent_run(
    runtime: DbosRuntime, *, granted: bool
) -> tuple[WorkflowRevision, AgentBindingSet]:
    """Publish the schema, grant, auth, configuration, and workflow for one run.

    With `granted`, the agent node pins the `open-pr` grant; without it, the same
    node declares no tools, so the tool exists only where a grant declared it.
    """
    for revision in (
        ANY_JSON_SCHEMA,
        PublishedRevision(RevisionKind.TOOL, OPEN_PR_GRANT),
    ):
        published = DbosCatalogStore(runtime.engine).publish_revision(revision)
        assert isinstance(
            published, (PublishedRevisionCreated, PublishedRevisionExisting)
        ), published
    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    auth = AuthProfileRevision("max", 1, ProviderId(PROVIDER), AuthMode.SUBSCRIPTION)
    assert isinstance(
        catalog.publish_auth_profile_revision(auth), AuthProfileRevisionCreated
    )
    configuration = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision(EXECUTOR_REVISION),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    assert isinstance(
        catalog.publish_agent_configuration_revision(configuration),
        AgentConfigurationRevisionCreated,
    )
    publish_checked_model_registry(
        runtime.engine, ProviderId(PROVIDER), (configuration,)
    )
    workflow = WorkflowRevision(
        _grant_document(_granted_tools_line() if granted else "")
    )
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    return workflow, AgentBindingSet(
        (AgentBinding(AgentRole(BUILDER_ROLE), configuration.revision_hash),)
    )


def create_open_pr_agent_run(
    runtime: DbosRuntime,
    run: RunId,
    workflow: WorkflowRevision,
    bindings: AgentBindingSet,
) -> None:
    """Create the durable run for this published workflow, without launching it."""
    started = DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        runtime.agent_executor_registry,
        effect_adapter_proves_absence=True,
    ).start_published(StartPublishedRunRequestV2(run, workflow.revision_hash, bindings))
    assert isinstance(started, DurableRunCreated), started


def complete_run(runtime: DbosRuntime, run: RunId) -> None:
    """Lift the run to its `COMPLETED` end without running the workflow behind it.

    The live-GitHub startup scan reads the run's own durable state, so a test
    about whether a finished run still blocks that start only needs the state
    to say so.
    """
    with runtime.engine.begin() as connection:
        connection.execute(
            sa.update(runs)
            .where(runs.c.run_id == run.value)
            .values(state=RunState.COMPLETED.value, terminal_hash="a" * 64)
        )


def seed_current_node_attempt(runtime: DbosRuntime, run: RunId, ordinal: int) -> str:
    """Seed the run's current node one attempt and return the workflow driving it.

    The live-GitHub startup scan asks `driving_workflow_ids` which workflows can
    still owe each attempt of the current node its next move, so a test that wants
    to leave a redemption owed seeds the real attempt the scan reads and takes the
    driving id from the same production owner. The seeded attempt is
    local-process-carried, so the first of those ids is the one that will ever
    hold a status: its node workflow, or its replacement workflow at ordinal two.
    """
    with runtime.engine.connect() as connection:
        row = (
            connection.execute(
                sa.select(
                    runs.c.revision_hash,
                    runs.c.current_node_id,
                    runs.c.current_round_ordinal,
                ).where(runs.c.run_id == run.value)
            )
            .mappings()
            .one()
        )
    revision_hash = WorkflowRevisionHash(str(row["revision_hash"]))
    node_id = str(row["current_node_id"])
    execution_id = NodeExecutionId.for_node(
        run, revision_hash, node_id, int(row["current_round_ordinal"])
    )
    request_hash = AgentExecutionRequestHash("b" * 64)
    attempt = AgentAttempt(
        AgentAttemptId.for_execution(execution_id, request_hash, ordinal),
        execution_id,
        request_hash,
        AgentExecutorOperationalIdentity("seed-executor"),
        run,
        revision_hash,
        node_id,
        ordinal,
        AgentAttemptState.PREPARED,
        0,
    )
    with runtime.engine.begin() as connection:
        connection.execute(
            agent_attempts.insert().values(
                attempt_id=attempt.attempt_id.value,
                node_execution_id=attempt.node_execution_id.value,
                request_hash=attempt.request_hash.value,
                executor_operational_identity=(
                    attempt.executor_operational_identity.value
                ),
                run_id=attempt.run_id.value,
                workflow_revision_hash=attempt.workflow_revision_hash.value,
                node_id=attempt.node_id,
                attempt_ordinal=attempt.attempt_ordinal,
                state=attempt.state.value,
                state_version=attempt.state_version,
                process_phase=attempt.process_phase.value,
                runner_evidence_acceptance_phase=(
                    attempt.runner_evidence_acceptance_phase.value
                ),
            )
        )
    return driving_workflow_ids(attempt)[0]


def seed_workflow_status(
    runtime: DbosRuntime,
    workflow_id: str,
    status: str,
    *,
    application_version: str | None = None,
) -> None:
    """Leave a workflow in the durable status a crash or a finish leaves.

    The durable runtime owns this table; a test that wants to ask about a
    workflow left mid-flight by a crash cannot reach that status by running one.

    The row carries the version that wrote it, because recovery does: DBOS
    resumes only workflows of the version it is running, so a scan asking whether
    anything still drives a run reads a retired row as dead. This seeds the
    runtime's own version unless a scenario names that retired case.
    """
    with runtime.engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO workflow_status "
                "(workflow_uuid, status, application_version, "
                "created_at, updated_at, priority) "
                "VALUES (:workflow_id, :status, :application_version, 0, 0, 0)"
            ),
            {
                "workflow_id": workflow_id,
                "status": status,
                "application_version": (
                    runtime.settings.application_version
                    if application_version is None
                    else application_version
                ),
            },
        )
