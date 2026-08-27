"""The public P3 line publishes its candidate before opening the matching PR."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Never

import pytest
import sqlalchemy as sa

from atelier2.adapters.candidate_store import CANDIDATE_STORE_DIRECTORY_NAME
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.effect_store import intent_snapshot_from_record
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import effect_intents, effect_receipts, runs
from atelier2.adapters.dbos.starter import DbosWorkflowRevisionPublisher
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.git_transport.effects import (
    GitRemote,
    GitTransportEffectAdapterFactory,
)
from atelier2.adapters.github.effects import GitHubEffectAdapterFactory
from atelier2.api.openapi import API_PREFIX
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
from atelier2.contracts.effect_requests import (
    OpenPullRequest,
    head_branch_for_queue_item,
)
from atelier2.contracts.effects import (
    AdapterRevision,
    EffectDestination,
    EffectIntentStateVersion,
    OperatorAuthoritativeAbsence,
    ReconcileActor,
    ReconcileCommand,
    ReconcileCommandId,
)
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.queue_projection import TrackerItemReference, WorkItemReference
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
from atelier2.contracts.tool_grants_v3 import ToolGrantCapability
from atelier2.contracts.when import RecordedAt
from atelier2.contracts.work_items import (
    WORK_ITEM_ORDER_SCHEMA_DOCUMENT,
    ObservedWorkItemRevision,
    WorkItemChangeMarker,
    WorkItemKind,
)
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AuthProfileRevisionCreated,
)
from atelier2.ports.effects import EffectAdapterRegistration, EffectAdapterRegistry
from atelier2.ports.issue_observation import WorkItemRevisionObserved
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
from tests.scenarios.runs import submit_reconcile_command
from tests.scenarios.workflows import ANY_JSON_SCHEMA, declared_output

PROJECT = ProjectId("p3-public")
ITEM = TrackerItemReference("gh:642")
RUN = RunId("v3/push-before-open-pr")
ORDER_NAME = "work-item"
AGENT_OUTPUT = b'{"summary":"publish the captured candidate"}'
_WRITE_CANDIDATE = (
    "import os,pathlib,sys;"
    "pathlib.Path('candidate.txt').write_bytes(bytes.fromhex(sys.argv[1]));"
    "os.write(1,bytes.fromhex(sys.argv[2]))"
)


def _git(repository: Path, *arguments: str) -> str:
    environment = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.test",
        "GIT_COMMITTER_NAME": "fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.test",
    }
    completed = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        env=environment,
        capture_output=True,
        check=True,
    )
    return completed.stdout.decode().strip()


def _repositories(root: Path) -> tuple[Path, Path, str]:
    project = root / "project"
    project.mkdir()
    _git(project, "init", "--quiet", "--initial-branch=main")
    (project / "base.txt").write_text("base\n", encoding="utf-8")
    _git(project, "add", "base.txt")
    _git(project, "commit", "--quiet", "-m", "base")
    base = _git(project, "rev-parse", "HEAD")
    remote = root / "remote.git"
    _git(root, "init", "--bare", "--quiet", str(remote))
    _git(project, "push", "--quiet", str(remote), "HEAD:refs/heads/main")
    return project, remote, base


@dataclass(frozen=True)
class _Tracker:
    revision: ObservedWorkItemRevision

    def open_items(self) -> Never:
        raise AssertionError("a public start reads only the named work item")

    def snapshot(self, reference: TrackerItemReference) -> WorkItemRevisionObserved:
        assert reference == self.revision.item
        return WorkItemRevisionObserved(self.revision)


def _publish(runtime: DbosRuntime) -> tuple[WorkflowRevision, AgentBindingSet]:
    push_operation = PublishedRevision(
        RevisionKind.ADAPTER_OPERATION,
        json.dumps(
            {
                "operation": AdapterOperationName.PUSH_ATELIER_COMMIT.value,
                "author": {
                    "name": "Atelier Agent",
                    "email": "agent@example.test",
                },
                "committer": {
                    "name": "Atelier Core",
                    "email": "core@example.test",
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
    )
    open_pr_operation = PublishedRevision(
        RevisionKind.ADAPTER_OPERATION,
        b'{"operation":"open-pr"}',
    )
    push_grant = PublishedRevision(
        RevisionKind.TOOL,
        json.dumps(
            {
                "capability": ToolGrantCapability.PUSH_ATELIER_COMMIT.value,
                "operation": {
                    "ref": "push-atelier-commit",
                    "revision": push_operation.revision_hash.value,
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
    )
    work_item_schema = PublishedRevision(
        RevisionKind.SCHEMA, WORK_ITEM_ORDER_SCHEMA_DOCUMENT
    )
    store = DbosCatalogStore(runtime.engine)
    for revision in (
        ANY_JSON_SCHEMA,
        work_item_schema,
        push_operation,
        open_pr_operation,
        push_grant,
    ):
        published = store.publish_revision(revision)
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
    publish_checked_model_registry(
        runtime.engine, ProviderId("exact"), (configuration,)
    )

    document = (
        f"""format_version: 3
name: Push before opening the pull request
graph_inputs:
  - name: {ORDER_NAME}
    schema:
      ref: work-item
      revision: {work_item_schema.revision_hash.value}
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Implement the named work item.
    tools:
      - {{ref: push-atelier-commit, revision: {push_grant.revision_hash.value}}}
    inputs:
      - name: {ORDER_NAME}
        from:
          graph_input: {ORDER_NAME}
""".encode()
        + declared_output()
        + f"""  - id: publish
    type: action
    operation: {{ref: open-pr, revision: {open_pr_operation.revision_hash.value}}}
    depends_on: [implement]
""".encode()
    )
    workflow = WorkflowRevision(document)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    return workflow, AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )


def _wait_for_state(runtime: DbosRuntime, expected: RunState) -> None:
    deadline = time.monotonic() + 10
    observed = ""
    while time.monotonic() < deadline:
        with runtime.engine.connect() as connection:
            observed = str(
                connection.scalar(
                    sa.select(runs.c.state).where(runs.c.run_id == RUN.value)
                )
            )
        if observed == expected.value:
            return
        time.sleep(0.025)
    raise AssertionError(f"run stayed {observed!r}, expected {expected.value!r}")


@pytest.mark.proves("an-authorised-candidate-is-pushed-before-its-pr-opens")
def test_public_start_pushes_the_candidate_before_opening_its_pull_request(
    tmp_path: Path,
) -> None:
    project, remote, base = _repositories(tmp_path)
    github = GitHubEffectAdapterFactory(
        tmp_path / "github.sqlite",
        AdapterRevision("github-open-pr-v1"),
        EffectDestination("platform"),
    )
    push = GitTransportEffectAdapterFactory(
        tmp_path / CANDIDATE_STORE_DIRECTORY_NAME,
        GitRemote("local-public-test", str(remote)),
        AdapterRevision("git-push-v1"),
        EffectDestination("git"),
    )
    registry = EffectAdapterRegistry(
        (
            EffectAdapterRegistration(AdapterOperationName.OPEN_PR, github),
            EffectAdapterRegistration(AdapterOperationName.PUSH_ATELIER_COMMIT, push),
        )
    )
    executor = RecordingAgentExecutorFactoryV2(
        "exact",
        "exact/v1",
        "exact-operation",
        AGENT_OUTPUT,
        command=launching(
            sys.executable,
            "-c",
            _WRITE_CANDIDATE,
            b"candidate exact bytes\n".hex(),
            AGENT_OUTPUT.hex(),
        ),
    )
    runtime = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "p3-public-test",
            agent_scratch_root=agent_scratch_root(tmp_path),
            project_id=PROJECT,
            bootstrap_project_root=project,
        ),
        registry,
        ExactOutputAgentExecutorFactory(),
        (executor,),
    )
    runtime.initialize_storage()
    try:
        workflow, bindings = _publish(runtime)
        item = ObservedWorkItemRevision(
            ITEM,
            WorkItemKind.ISSUE,
            b"Implement P3.",
            WorkItemChangeMarker("issue-642-v1"),
            RecordedAt("2026-08-27T12:00:00Z"),
        )
        binding = bindings.bindings[0]
        client = durable_api_client(
            runtime,
            served_project_id=PROJECT,
            tracker_item_source=_Tracker(item),
        )
        response = client.post(
            API_PREFIX + "/runs",
            json={
                "workflow_format_version": 3,
                "run_id": RUN.value,
                "workflow_revision_hash": workflow.revision_hash.value,
                "agent_bindings": [
                    {
                        "role": binding.role.value,
                        "agent_configuration_revision_hash": (
                            binding.agent_configuration_revision_hash.value
                        ),
                    }
                ],
                "orders": [{"name": ORDER_NAME, "work_item": ITEM.value}],
            },
        )
        assert response.status_code == 201, response.text
        runtime.launch()
        _wait_for_state(runtime, RunState.WAITING_RECONCILIATION)

        with runtime.engine.connect() as connection:
            push_intent = intent_snapshot_from_record(
                connection.execute(
                    sa.select(effect_intents).where(
                        effect_intents.c.operation_name
                        == AdapterOperationName.PUSH_ATELIER_COMMIT.value
                    )
                )
                .mappings()
                .one()
            ).intent
        submit_reconcile_command(
            runtime.engine,
            runtime.settings,
            ReconcileCommand(
                ReconcileCommandId("authorize-public-p3-push"),
                push_intent.reference,
                EffectIntentStateVersion(1),
                ReconcileActor("operator"),
                "confirmed the derived branch is absent on the connected remote",
                OperatorAuthoritativeAbsence(),
            ),
        )
        _wait_for_state(runtime, RunState.COMPLETED)

        branch = head_branch_for_queue_item(
            WorkItemReference(PROJECT, ITEM).item_id
        ).value
        commit = _git(remote, "rev-parse", f"refs/heads/{branch}")
        pushed_tree = _git(remote, "rev-parse", f"{commit}^{{tree}}")
        assert _git(remote, "rev-parse", f"{commit}^") == base
        assert _git(remote, "show", f"{commit}:candidate.txt") == (
            "candidate exact bytes"
        )

        recorded_pull_requests = github.recorded_pull_requests()
        assert len(recorded_pull_requests) == 1
        pull_request = recorded_pull_requests[0]
        assert pull_request.branch == branch

        with runtime.engine.connect() as connection:
            intent_rows = connection.execute(
                sa.select(effect_intents).order_by(sa.literal_column("rowid"))
            ).mappings()
            intents = tuple(
                intent_snapshot_from_record(row).intent for row in intent_rows
            )
            receipts = connection.execute(
                sa.select(
                    effect_receipts.c.operation_name,
                    effect_receipts.c.result,
                ).order_by(sa.literal_column("rowid"))
            ).all()
        assert [intent.binding.operation_name for intent in intents] == [
            AdapterOperationName.PUSH_ATELIER_COMMIT,
            AdapterOperationName.OPEN_PR,
        ]
        assert [row.operation_name for row in receipts] == [
            AdapterOperationName.PUSH_ATELIER_COMMIT.value,
            AdapterOperationName.OPEN_PR.value,
        ]
        push_receipt = json.loads(bytes(receipts[0].result).decode("utf-8"))
        assert push_receipt["commit_oid"] == commit
        assert push_receipt["candidate_tree"] == pushed_tree
        open_request = OpenPullRequest.from_canonical_bytes(intents[1].request.payload)
        assert open_request.head_branch.value == branch
    finally:
        runtime.close()
