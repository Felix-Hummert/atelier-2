"""A V3 agent node opens its own pull request through a declared `open-pr` grant.

`#431` Phase 2: the pull request the Action node lands in `test_v3_open_pr_action`
is here opened by the agent node itself, as a declared tool grant rather than a
downstream Action. The same locked adapter, the same receipt, no `project_source`
the grant has no use for, and no token in anything durable. Without the grant the
tool does not exist: a plain agent node opens nothing.

The proof is the whole vertical, driven from the public start seam and read back
from the store, because each half alone is a promise -- an admitted `open-pr`
grant nothing redeems, or a pull request no grant could have asked for.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa

from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.effect_store import intent_snapshot_from_record
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import (
    effect_intents,
    effect_receipts,
    run_events,
    runs,
    tool_redemptions,
)
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.github.effects import GitHubEffectAdapterFactory
from atelier2.adapters.github.marker import body_carries_request_hash
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
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.executions import RunEventKind
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
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
from tests.scenarios.agents import RecordingAgentExecutorFactoryV2, agent_scratch_root
from tests.scenarios.workflows import ANY_JSON_SCHEMA, declared_output

RUN = RunId("v3/agent-open-pr")
UNGRANTED_RUN = RunId("v3/agent-no-grant")
PR_SPEC = json.dumps(
    {"title": "Ship the open-pr slice", "opened_by": "the agent itself"}
).encode("utf-8")
CANARY_TOKEN = "gho_atelier2_canary_token_must_not_appear"
OPEN_PR_GRANT = json.dumps({"capability": ToolGrantCapability.OPEN_PR.value}).encode(
    "utf-8"
)


@pytest.fixture
def runtime(
    tmp_path: Path,
) -> Iterator[tuple[DbosRuntime, GitHubEffectAdapterFactory, Path]]:
    github = GitHubEffectAdapterFactory(
        tmp_path / "github.sqlite",
        AdapterRevision("github-open-pr-v1"),
        EffectDestination("platform"),
    )
    started = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "v3-agent-open-pr-test",
            agent_scratch_root=agent_scratch_root(tmp_path),
        ),
        github,
        ExactOutputAgentExecutorFactory(),
        (RecordingAgentExecutorFactoryV2("exact", "exact/v1", "exact-op", PR_SPEC),),
    )
    started.initialize_storage()
    try:
        yield started, github, tmp_path / "atelier.sqlite"
    finally:
        started.close()


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


def _publish(
    runtime: DbosRuntime, tools_line: str
) -> tuple[WorkflowRevision, AgentBindingSet]:
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
    auth = AuthProfileRevision("max", 1, ProviderId("exact"), AuthMode.SUBSCRIPTION)
    assert isinstance(
        catalog.publish_auth_profile_revision(auth), AuthProfileRevisionCreated
    )
    configuration = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision("exact/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    assert isinstance(
        catalog.publish_agent_configuration_revision(configuration),
        AgentConfigurationRevisionCreated,
    )
    workflow = WorkflowRevision(_grant_document(tools_line))
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    return workflow, AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )


def _granted_tools_line() -> str:
    revision = PublishedRevision(RevisionKind.TOOL, OPEN_PR_GRANT).revision_hash.value
    return f"    tools:\n      - {{ref: open-pr, revision: {revision}}}\n"


def _start(
    runtime: DbosRuntime,
    run: RunId,
    workflow: WorkflowRevision,
    bindings: AgentBindingSet,
) -> None:
    started = DbosDurableRunStarter(
        runtime.engine, runtime.settings, runtime.agent_executor_registry
    ).start_published(StartPublishedRunRequestV2(run, workflow.revision_hash, bindings))
    assert isinstance(started, DurableRunCreated), started
    runtime.launch()


def _wait_for_state(runtime: DbosRuntime, run: RunId, state: RunState) -> None:
    deadline = time.monotonic() + 15
    observed = ""
    while time.monotonic() < deadline:
        with runtime.engine.connect() as connection:
            observed = str(
                connection.scalar(
                    sa.select(runs.c.state).where(runs.c.run_id == run.value)
                )
            )
        if observed == state.value:
            return
        time.sleep(0.025)
    raise AssertionError(f"run stayed {observed!r}, expected {state.value!r}")


def _wait_for_receipt(runtime: DbosRuntime) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        with runtime.engine.connect() as connection:
            if connection.scalar(
                sa.select(sa.func.count()).select_from(effect_receipts)
            ):
                return
        time.sleep(0.025)
    raise AssertionError("no effect receipt was written")


def _durable_bytes_contain(database: Path, token: str) -> bool:
    needle = token.encode("utf-8")
    for candidate in (
        database,
        database.with_name(database.name + "-wal"),
        database.with_name(database.name + "-shm"),
    ):
        if candidate.is_file() and needle in candidate.read_bytes():
            return True
    return False


@pytest.mark.proves("a-v3-agent-node-opens-one-pr-through-its-own-open-pr-grant")
def test_a_granted_agent_node_opens_one_pull_request_and_leaves_one_receipt(
    runtime: tuple[DbosRuntime, GitHubEffectAdapterFactory, Path],
) -> None:
    started_runtime, github, atelier_sqlite = runtime
    workflow, bindings = _publish(started_runtime, _granted_tools_line())

    _start(started_runtime, RUN, workflow, bindings)
    _wait_for_state(started_runtime, RUN, RunState.COMPLETED)
    _wait_for_receipt(started_runtime)

    recorded = github.recorded_pull_requests()
    assert len(recorded) == 1
    pull_request = recorded[0]

    with started_runtime.engine.connect() as connection:
        agent_output = bytes(
            connection.execute(
                sa.select(run_events.c.payload).where(
                    run_events.c.run_id == RUN.value,
                    run_events.c.event_kind == RunEventKind.AGENT_COMPLETED.value,
                )
            ).scalar_one()
        )
        intent = intent_snapshot_from_record(
            connection.execute(sa.select(effect_intents)).mappings().one()
        ).intent
        receipt_payload = bytes(
            connection.execute(sa.select(effect_receipts.c.result)).scalar_one()
        )
        redemption_count = connection.scalar(
            sa.select(sa.func.count()).select_from(tool_redemptions)
        )

    # The pull request is the agent's own kept output, marked by this exact
    # prepared request -- the same shape an Action's confirmation leaves.
    assert agent_output == PR_SPEC
    assert intent.request.payload == PR_SPEC
    assert body_carries_request_hash(
        pull_request.body, intent.request.request_hash.value
    )
    assert json.loads(receipt_payload.decode("utf-8")) == {
        "branch": pull_request.branch,
        "pr_number": pull_request.pr_number,
    }
    # An open-pr grant is redeemed as a platform effect, never into the
    # exec-shaped tool_redemptions row a verification leaves.
    assert redemption_count == 0

    # Idempotency: the derived logical key readback-matches the first pull
    # request, so redeeming again opens no twin.
    adapter = github.open()
    try:
        replayed = adapter.execute(intent)
    finally:
        adapter.close()
    assert json.loads(replayed.result.payload.decode("utf-8")) == {
        "branch": pull_request.branch,
        "pr_number": pull_request.pr_number,
    }
    assert len(github.recorded_pull_requests()) == 1

    # No credential-shaped value reaches anything durable the redemption touched.
    assert CANARY_TOKEN not in pull_request.body
    assert not _durable_bytes_contain(atelier_sqlite, CANARY_TOKEN)
    assert not _durable_bytes_contain(github.database_path, CANARY_TOKEN)


@pytest.mark.proves("without-the-grant-the-open-pr-tool-does-not-exist")
def test_an_agent_node_without_the_grant_opens_no_pull_request(
    runtime: tuple[DbosRuntime, GitHubEffectAdapterFactory, Path],
) -> None:
    """A plain agent node has no open-pr tool: it completes and opens nothing."""
    started_runtime, github, _atelier_sqlite = runtime
    workflow, bindings = _publish(started_runtime, "")

    _start(started_runtime, UNGRANTED_RUN, workflow, bindings)
    _wait_for_state(started_runtime, UNGRANTED_RUN, RunState.COMPLETED)

    assert github.recorded_pull_requests() == ()
    with started_runtime.engine.connect() as connection:
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(effect_intents))
            == 0
        )
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(effect_receipts))
            == 0
        )
