"""A V3 Agent→Action line lands one pull request through the GitHub adapter."""

from __future__ import annotations

import json
import sys
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
    run_forks,
    runs,
)
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.github.effects import (
    GitHubEffectAdapterFactory,
    RecordedPullRequest,
)
from atelier2.adapters.github.marker import body_carries_request_hash
from atelier2.api.openapi import API_PREFIX
from atelier2.api.references import encode_public_run_reference
from atelier2.contracts.adapter_operations_v3 import AdapterOperationName
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
from atelier2.contracts.effects import (
    AdapterRevision,
    EffectAdapterBinding,
    EffectDestination,
    EffectIntent,
    EffectReadback,
    PerformedEffect,
)
from atelier2.contracts.executions import RunEventKind
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.run_forks import RunForkCommandId, successor_run_id_for
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AuthProfileRevisionCreated,
)
from atelier2.ports.durable_run_forks import (
    DurableRunForkCreated,
    DurableRunForkStateCorrupt,
    ForkRunRequest,
)
from atelier2.ports.durable_runs import DurableRunCreated, StartPublishedRunRequestV2
from atelier2.ports.effects import EffectAdapter
from atelier2.ports.published_revisions import (
    PublishedRevisionCreated,
    PublishedRevisionExisting,
)
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_scratch_root,
    launching,
    publish_checked_model_registry,
)
from tests.scenarios.api import durable_api_client
from tests.scenarios.workflows import ANY_JSON_SCHEMA, declared_output

RUN = RunId("v3/open-pr")
TREE = json.dumps({"files": {"hello.txt": "from the builder"}}).encode("utf-8")
CANARY_TOKEN = "gho_atelier2_canary_token_must_not_appear"
OPEN_PR_DOCUMENT = json.dumps({"operation": AdapterOperationName.OPEN_PR.value}).encode(
    "utf-8"
)
_LIST_THEN_WRITE = (
    "import os, pathlib, sys; "
    "pathlib.Path(sys.argv[1]).write_text('\\n'.join(sorted(os.listdir('.')))); "
    "os.write(1, bytes.fromhex(sys.argv[2]))"
)


class CountingGitHubEffectAdapter:
    def __init__(
        self, owner: CountingGitHubEffectAdapterFactory, delegate: EffectAdapter
    ) -> None:
        self._owner = owner
        self._delegate = delegate

    def readback(self, intent: EffectIntent) -> EffectReadback:
        self._owner.readback_calls += 1
        return self._delegate.readback(intent)

    def execute(self, intent: EffectIntent) -> PerformedEffect:
        self._owner.execute_calls += 1
        return self._delegate.execute(intent)

    def close(self) -> None:
        self._delegate.close()


class CountingGitHubEffectAdapterFactory:
    def __init__(self, database: Path) -> None:
        self._delegate = GitHubEffectAdapterFactory(
            database,
            AdapterRevision("github-open-pr-v1"),
            EffectDestination("platform"),
        )
        self.readback_calls = 0
        self.execute_calls = 0

    @property
    def database_path(self) -> Path:
        return self._delegate.database_path

    @property
    def binding(self) -> EffectAdapterBinding:
        return self._delegate.binding

    @property
    def proves_absence(self) -> bool:
        return self._delegate.proves_absence

    def open(self) -> CountingGitHubEffectAdapter:
        return CountingGitHubEffectAdapter(self, self._delegate.open())

    def recorded_pull_requests(self) -> tuple[RecordedPullRequest, ...]:
        return self._delegate.recorded_pull_requests()


@pytest.fixture
def runtime(
    tmp_path: Path,
) -> Iterator[tuple[DbosRuntime, CountingGitHubEffectAdapterFactory, Path, Path]]:
    listing = tmp_path / "lease-listing.txt"
    recording = RecordingAgentExecutorFactoryV2(
        "exact",
        "exact/v1",
        "exact-operation",
        TREE,
        command=launching(
            sys.executable,
            "-c",
            _LIST_THEN_WRITE,
            str(listing),
            TREE.hex(),
        ),
    )
    github = CountingGitHubEffectAdapterFactory(tmp_path / "github.sqlite")
    started = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "v3-open-pr-test",
            agent_scratch_root=agent_scratch_root(tmp_path),
        ),
        github,
        ExactOutputAgentExecutorFactory(),
        (recording,),
    )
    started.initialize_storage()
    try:
        yield started, github, listing, tmp_path / "atelier.sqlite"
    finally:
        started.close()


def publish_line(
    runtime: DbosRuntime, *, action_successor: bool = False
) -> tuple[WorkflowRevision, AgentBindingSet]:
    catalog_store = DbosCatalogStore(runtime.engine)
    for revision in (
        ANY_JSON_SCHEMA,
        PublishedRevision(RevisionKind.ADAPTER_OPERATION, OPEN_PR_DOCUMENT),
    ):
        published = catalog_store.publish_revision(revision)
        assert isinstance(
            published, (PublishedRevisionCreated, PublishedRevisionExisting)
        ), published
    operation_hash = PublishedRevision(
        RevisionKind.ADAPTER_OPERATION, OPEN_PR_DOCUMENT
    ).revision_hash.value
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
    publish_checked_model_registry(
        runtime.engine, ProviderId("exact"), (configuration,)
    )
    document = (
        b"""format_version: 3
name: Land the tree
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Write the tree this chain lands.
"""
        + declared_output()
        + f"""  - id: publish
    type: action
    operation: {{ref: open-pr, revision: {operation_hash}}}
    depends_on: [implement]
""".encode()
        + (
            b"""  - id: review
    type: agent
    role: builder
    mode: headless
    instruction: Review the confirmed publication.
    depends_on: [publish]
"""
            + declared_output()
            if action_successor
            else b""
        )
    )
    workflow = WorkflowRevision(document)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    return workflow, AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )


def wait_for_state(runtime: DbosRuntime, state: RunState, run_id: RunId = RUN) -> None:
    deadline = time.monotonic() + 8
    observed = ""
    while time.monotonic() < deadline:
        with runtime.engine.connect() as connection:
            observed = str(
                connection.scalar(
                    sa.select(runs.c.state).where(runs.c.run_id == run_id.value)
                )
            )
        if observed == state.value:
            return
        time.sleep(0.025)
    raise AssertionError(f"run stayed {observed!r}, expected {state.value!r}")


def test_forked_action_references_the_confirmed_pull_request_without_replaying_it(
    runtime: tuple[DbosRuntime, CountingGitHubEffectAdapterFactory, Path, Path],
) -> None:
    started_runtime, github, _listing, _atelier_sqlite = runtime
    workflow, bindings = publish_line(started_runtime)
    starter = DbosDurableRunStarter(
        started_runtime.engine,
        started_runtime.settings,
        started_runtime.agent_executor_registry,
    )
    started = starter.start_published(
        StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings)
    )
    assert isinstance(started, DurableRunCreated)
    started_runtime.launch()
    wait_for_state(started_runtime, RunState.COMPLETED)
    calls_before_fork = (github.readback_calls, github.execute_calls)

    request = ForkRunRequest(RUN, "retry-publish", "publish")
    forked = starter.fork_run(request)
    assert isinstance(forked, DurableRunForkCreated)
    successor = successor_run_id_for(RunForkCommandId.for_request(RUN, "retry-publish"))
    wait_for_state(started_runtime, RunState.COMPLETED, successor)

    assert len(github.recorded_pull_requests()) == 1
    assert (github.readback_calls, github.execute_calls) == calls_before_fork
    with started_runtime.engine.connect() as connection:
        successor_receipt = (
            connection.execute(
                sa.select(effect_receipts).where(
                    effect_receipts.c.run_id == successor.value
                )
            )
            .mappings()
            .one()
        )
        successor_events = tuple(
            connection.execute(
                sa.select(run_events.c.event_kind).where(
                    run_events.c.run_id == successor.value
                )
            ).scalars()
        )
    assert successor_events == (RunEventKind.ACTION_COMPLETED.value,)
    assert successor_receipt["confirmation_source"] == "FORK_REFERENCE"
    assert successor_receipt["fork_source_run_id"] == RUN.value
    assert successor_receipt["fork_source_logical_key"] is not None
    assert successor_receipt["fork_source_result_hash"] is not None


def test_fork_reuses_a_successfully_confirmed_action_before_the_target(
    runtime: tuple[DbosRuntime, CountingGitHubEffectAdapterFactory, Path, Path],
) -> None:
    started_runtime, github, _listing, _atelier_sqlite = runtime
    workflow, bindings = publish_line(started_runtime, action_successor=True)
    starter = DbosDurableRunStarter(
        started_runtime.engine,
        started_runtime.settings,
        started_runtime.agent_executor_registry,
    )
    assert isinstance(
        starter.start_published(
            StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings)
        ),
        DurableRunCreated,
    )
    started_runtime.launch()
    wait_for_state(started_runtime, RunState.COMPLETED)
    calls_before_fork = (github.readback_calls, github.execute_calls)

    forked = starter.fork_run(ForkRunRequest(RUN, "reuse-action", "review"))

    assert isinstance(forked, DurableRunForkCreated)
    assert tuple(entry.node_id for entry in forked.fork.reused_nodes) == (
        "implement",
        "publish",
    )
    wait_for_state(started_runtime, RunState.COMPLETED, forked.run.run_id)
    assert (github.readback_calls, github.execute_calls) == calls_before_fork
    assert len(github.recorded_pull_requests()) == 1


def test_action_fork_with_changed_request_waits_without_invoking_the_adapter(
    runtime: tuple[DbosRuntime, CountingGitHubEffectAdapterFactory, Path, Path],
) -> None:
    started_runtime, github, _listing, _atelier_sqlite = runtime
    workflow, bindings = publish_line(started_runtime)
    starter = DbosDurableRunStarter(
        started_runtime.engine,
        started_runtime.settings,
        started_runtime.agent_executor_registry,
    )
    assert isinstance(
        starter.start_published(
            StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings)
        ),
        DurableRunCreated,
    )
    started_runtime.launch()
    wait_for_state(started_runtime, RunState.COMPLETED)
    calls_before_fork = (github.readback_calls, github.execute_calls)
    factory = next(
        entry.factory
        for entry in started_runtime.agent_executor_registry.entries
        if isinstance(entry.factory, RecordingAgentExecutorFactoryV2)
    )
    assert isinstance(factory, RecordingAgentExecutorFactoryV2)
    assert factory.opened is not None
    changed_tree = json.dumps({"files": {"hello.txt": "changed by the fork"}}).encode()
    factory.opened.command = launching(
        sys.executable,
        "-c",
        _LIST_THEN_WRITE,
        str(_listing),
        changed_tree.hex(),
    )

    forked = starter.fork_run(ForkRunRequest(RUN, "changed-action", "implement"))
    assert isinstance(forked, DurableRunForkCreated)
    wait_for_state(started_runtime, RunState.WAITING_RECONCILIATION, forked.run.run_id)

    assert (github.readback_calls, github.execute_calls) == calls_before_fork
    assert len(github.recorded_pull_requests()) == 1


def test_missing_confirmed_action_receipt_refuses_the_fork_before_any_side_effect(
    runtime: tuple[DbosRuntime, CountingGitHubEffectAdapterFactory, Path, Path],
) -> None:
    started_runtime, github, _listing, _atelier_sqlite = runtime
    workflow, bindings = publish_line(started_runtime)
    starter = DbosDurableRunStarter(
        started_runtime.engine,
        started_runtime.settings,
        started_runtime.agent_executor_registry,
    )
    assert isinstance(
        starter.start_published(
            StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings)
        ),
        DurableRunCreated,
    )
    started_runtime.launch()
    wait_for_state(started_runtime, RunState.COMPLETED)
    calls_before_fork = (github.readback_calls, github.execute_calls)
    with started_runtime.engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql("DROP TRIGGER effect_receipts_no_delete")
        connection.execute(
            effect_receipts.delete().where(effect_receipts.c.run_id == RUN.value)
        )
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")

    refused = starter.fork_run(ForkRunRequest(RUN, "missing-action-receipt", "publish"))

    assert isinstance(refused, DurableRunForkStateCorrupt)
    assert (github.readback_calls, github.execute_calls) == calls_before_fork
    with started_runtime.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(run_forks)) == 0


def test_unreached_action_without_a_receipt_does_not_block_a_full_fork(
    runtime: tuple[DbosRuntime, CountingGitHubEffectAdapterFactory, Path, Path],
) -> None:
    started_runtime, github, _listing, _atelier_sqlite = runtime
    workflow, bindings = publish_line(started_runtime)
    starter = DbosDurableRunStarter(
        started_runtime.engine,
        started_runtime.settings,
        started_runtime.agent_executor_registry,
    )
    factory = next(
        entry.factory
        for entry in started_runtime.agent_executor_registry.entries
        if isinstance(entry.factory, RecordingAgentExecutorFactoryV2)
    )
    assert isinstance(factory, RecordingAgentExecutorFactoryV2)
    assert factory.opened is not None
    factory.opened.command = launching(
        sys.executable,
        "-c",
        _LIST_THEN_WRITE,
        str(_listing),
        b"not-json".hex(),
    )
    assert isinstance(
        starter.start_published(
            StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings)
        ),
        DurableRunCreated,
    )
    started_runtime.launch()
    wait_for_state(started_runtime, RunState.FAILED)

    forked = starter.fork_run(ForkRunRequest(RUN, "unreached-action", "implement"))

    assert isinstance(forked, DurableRunForkCreated)
    assert github.readback_calls == github.execute_calls == 0


def durable_bytes_contain(database: Path, token: str) -> bool:
    needle = token.encode("utf-8")
    for candidate in (
        database,
        database.with_name(database.name + "-wal"),
        database.with_name(database.name + "-shm"),
    ):
        if candidate.is_file() and needle in candidate.read_bytes():
            return True
    return False


@pytest.mark.proves("a-v3-action-opens-one-pr-and-a-replay-does-not-create-a-twin")
def test_a_v3_agent_then_action_opens_one_pull_request_through_the_github_adapter(
    runtime: tuple[DbosRuntime, CountingGitHubEffectAdapterFactory, Path, Path],
) -> None:
    started_runtime, github, listing, atelier_sqlite = runtime
    workflow, bindings = publish_line(started_runtime)

    started = DbosDurableRunStarter(
        started_runtime.engine,
        started_runtime.settings,
        started_runtime.agent_executor_registry,
    ).start_published(StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings))
    assert isinstance(started, DurableRunCreated)

    started_runtime.launch()
    wait_for_state(started_runtime, RunState.COMPLETED)

    recorded = github.recorded_pull_requests()
    assert len(recorded) == 1
    pull_request = recorded[0]
    assert pull_request.pr_number == 1
    with started_runtime.engine.connect() as connection:
        events = [
            (
                str(record["node_id"]),
                str(record["event_kind"]),
                bytes(record["payload"]),
            )
            for record in connection.execute(
                sa.select(run_events)
                .where(run_events.c.run_id == RUN.value)
                .order_by(run_events.c.event_sequence)
            ).mappings()
        ]
        intent = intent_snapshot_from_record(
            connection.execute(sa.select(effect_intents)).mappings().one()
        ).intent
        receipt_payload = bytes(
            connection.execute(sa.select(effect_receipts.c.result)).scalar_one()
        )
    assert events[0][:2] == ("implement", RunEventKind.AGENT_COMPLETED.value)
    assert events[0][2] == TREE
    assert events[1][:2] == ("publish", RunEventKind.ACTION_COMPLETED.value)
    result = json.loads(events[1][2].decode("utf-8"))
    assert result == {"branch": pull_request.branch, "pr_number": 1}
    assert body_carries_request_hash(
        pull_request.body, intent.request.request_hash.value
    )
    assert json.loads(receipt_payload.decode("utf-8")) == result

    adapter = github.open()
    try:
        replayed = adapter.execute(intent)
    finally:
        adapter.close()
    assert json.loads(replayed.result.payload.decode("utf-8")) == result
    assert len(github.recorded_pull_requests()) == 1

    assert listing.is_file()
    names = listing.read_text().splitlines()
    assert ".git" not in names
    assert CANARY_TOKEN not in listing.read_text()
    assert CANARY_TOKEN not in pull_request.body
    assert CANARY_TOKEN.encode() not in events[1][2]
    assert CANARY_TOKEN.encode() not in receipt_payload
    assert not durable_bytes_contain(atelier_sqlite, CANARY_TOKEN)
    assert not durable_bytes_contain(github.database_path, CANARY_TOKEN)

    api = durable_api_client(started_runtime)
    public_ref = encode_public_run_reference(RUN)
    run = api.get(f"{API_PREFIX}/runs/{public_ref}")
    assert run.status_code == 200, run.text
    assert CANARY_TOKEN not in run.text
    stream = api.get(f"{API_PREFIX}/runs/{public_ref}/events")
    assert stream.status_code == 200, stream.text
    assert CANARY_TOKEN not in stream.text
    streamed = [
        json.loads(line.removeprefix("data: "))
        for line in stream.text.splitlines()
        if line.startswith("data: ")
    ]
    assert streamed[-1]["event"] == "ACTION_COMPLETED"
    assert streamed[-1]["workflow_format_version"] == 3
    assert "receipt" in streamed[-1]
    receipt = api.get(f"{API_PREFIX}/runs/{public_ref}/receipt")
    assert receipt.status_code == 200, receipt.text
    assert CANARY_TOKEN not in receipt.text
