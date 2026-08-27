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
    runs,
)
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.github.effects import GitHubEffectAdapterFactory
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
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.executions import RunEventKind
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
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


@pytest.fixture
def runtime(
    tmp_path: Path,
) -> Iterator[tuple[DbosRuntime, GitHubEffectAdapterFactory, Path, Path]]:
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
    github = GitHubEffectAdapterFactory(
        tmp_path / "github.sqlite",
        AdapterRevision("github-open-pr-v1"),
        EffectDestination("platform"),
    )
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
    runtime: DbosRuntime,
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
    )
    workflow = WorkflowRevision(document)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    return workflow, AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )


def wait_for_state(runtime: DbosRuntime, state: RunState) -> None:
    deadline = time.monotonic() + 8
    observed = ""
    while time.monotonic() < deadline:
        with runtime.engine.connect() as connection:
            observed = str(
                connection.scalar(
                    sa.select(runs.c.state).where(runs.c.run_id == RUN.value)
                )
            )
        if observed == state.value:
            return
        time.sleep(0.025)
    raise AssertionError(f"run stayed {observed!r}, expected {state.value!r}")


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
    runtime: tuple[DbosRuntime, GitHubEffectAdapterFactory, Path, Path],
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
