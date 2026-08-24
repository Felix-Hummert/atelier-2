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

from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutorRevision,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.runs import RunId, WorkflowRevision
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
from tests.scenarios.agents import RecordingAgentExecutorFactoryV2
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
        runtime.engine, runtime.settings, runtime.agent_executor_registry
    ).start_published(StartPublishedRunRequestV2(run, workflow.revision_hash, bindings))
    assert isinstance(started, DurableRunCreated), started
