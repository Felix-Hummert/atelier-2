"""The repository workflow publishes its candidate before opening its pull request."""

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
from atelier2.adapters.dbos.advancer import RunEffectConflict, graph_action_intent
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
from atelier2.adapters.yaml_workflows import parse_workflow_document
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
    GitCommitIdentity,
    OpenPullRequest,
    PushAtelierCommitReceipt,
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
from atelier2.contracts.queue_projection import TrackerItemReference
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
from atelier2.contracts.workflows_v3 import AgentNodeV3
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

WORKFLOW_PATH = Path("workflows/push-before-open-pr.yaml")
PROJECT = ProjectId("push-before-open-pr-workflow")
ITEM = TrackerItemReference("gh:883")
RUN = RunId("v3/repository-push-before-open-pr")
AGENT_OUTPUT = b'"publish this candidate"'
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


def _publish_workflow(
    runtime: DbosRuntime,
) -> tuple[
    WorkflowRevision,
    AgentBindingSet,
    tuple[GitCommitIdentity, GitCommitIdentity],
]:
    # The published live revisions pin the operator as author and the pushing
    # node's model as committer (issue #883, operator ruling 30.08.2026); the
    # shipped document binds difficulty 2, which the project defaults answer
    # with grok-4.6. Reproducing that exact pair is what makes the derived
    # grant hash equal the one the shipped document pins.
    connected_account_address = "44832414+FlexOr2@users.noreply.github.com"
    author = GitCommitIdentity("Felix Hummert", connected_account_address)
    pushing_model = "grok-4.6"
    committer = GitCommitIdentity("Grok 4.6", connected_account_address)
    push_operation = PublishedRevision(
        RevisionKind.ADAPTER_OPERATION,
        json.dumps(
            {
                "operation": AdapterOperationName.PUSH_ATELIER_COMMIT.value,
                "author": author.as_json(),
                "committer": committer.as_json(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
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
    revisions = (
        PublishedRevision(
            RevisionKind.SCHEMA,
            Path("workflows/schemas/nonempty_string.json").read_bytes(),
        ),
        PublishedRevision(RevisionKind.SCHEMA, WORK_ITEM_ORDER_SCHEMA_DOCUMENT),
        push_operation,
        PublishedRevision(RevisionKind.ADAPTER_OPERATION, b'{"operation":"open-pr"}'),
        push_grant,
    )
    store = DbosCatalogStore(runtime.engine)
    for revision in revisions:
        published = store.publish_revision(revision)
        assert isinstance(
            published, (PublishedRevisionCreated, PublishedRevisionExisting)
        ), published

    shipped_document = WORKFLOW_PATH.read_bytes()
    assert push_grant.revision_hash.value.encode() in shipped_document
    (pushing_node,) = (
        node
        for node in parse_workflow_document(shipped_document).nodes
        if isinstance(node, AgentNodeV3)
    )
    assert pushing_node.model == pushing_model
    workflow = WorkflowRevision(shipped_document)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)

    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    auth = AuthProfileRevision(
        "workflow-test", 1, ProviderId("exact"), AuthMode.SUBSCRIPTION
    )
    assert isinstance(
        catalog.publish_auth_profile_revision(auth), AuthProfileRevisionCreated
    )
    configuration = AgentConfigurationRevision(
        "builder",
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
    bindings = AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )
    return workflow, bindings, (author, committer)


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
def test_repository_workflow_binds_open_pr_to_its_confirmed_push_receipt(
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
        GitRemote("local-workflow-test", str(remote)),
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
            "repository-workflow-test",
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
        workflow, bindings, (author, committer) = _publish_workflow(runtime)
        item = ObservedWorkItemRevision(
            ITEM,
            WorkItemKind.ISSUE,
            b"Implement the repository workflow proof.",
            WorkItemChangeMarker("issue-883-v1"),
            RecordedAt("2026-08-29T12:00:00Z"),
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
                "orders": [{"name": "work_item", "work_item": ITEM.value}],
            },
        )
        assert response.status_code == 201, response.text
        runtime.launch()
        _wait_for_state(runtime, RunState.WAITING_RECONCILIATION)

        with runtime.engine.connect() as connection:
            waiting_intents = connection.execute(
                sa.select(effect_intents).order_by(sa.literal_column("rowid"))
            ).mappings()
            intents = tuple(
                intent_snapshot_from_record(row).intent for row in waiting_intents
            )
            receipt_count = connection.scalar(
                sa.select(sa.func.count()).select_from(effect_receipts)
            )
            current_node = connection.scalar(
                sa.select(runs.c.current_node_id).where(runs.c.run_id == RUN.value)
            )
        assert [intent.binding.operation_name for intent in intents] == [
            AdapterOperationName.PUSH_ATELIER_COMMIT
        ]
        assert receipt_count == 0
        assert current_node == "implement"
        assert github.recorded_pull_requests() == ()

        push_intent = intents[0]
        submit_reconcile_command(
            runtime.engine,
            runtime.settings,
            ReconcileCommand(
                ReconcileCommandId("authorize-workflow-push"),
                push_intent.reference,
                EffectIntentStateVersion(1),
                ReconcileActor("operator"),
                "confirmed the derived branch is absent on the connected remote",
                OperatorAuthoritativeAbsence(),
            ),
        )
        _wait_for_state(runtime, RunState.COMPLETED)

        with runtime.engine.connect() as connection:
            final_intents = tuple(
                intent_snapshot_from_record(row).intent
                for row in connection.execute(
                    sa.select(effect_intents).order_by(sa.literal_column("rowid"))
                ).mappings()
            )
            receipts = connection.execute(
                sa.select(
                    effect_receipts.c.operation_name,
                    effect_receipts.c.result,
                ).order_by(sa.literal_column("rowid"))
            ).all()
        assert [intent.binding.operation_name for intent in final_intents] == [
            AdapterOperationName.PUSH_ATELIER_COMMIT,
            AdapterOperationName.OPEN_PR,
        ]
        assert [receipt.operation_name for receipt in receipts] == [
            AdapterOperationName.PUSH_ATELIER_COMMIT.value,
            AdapterOperationName.OPEN_PR.value,
        ]

        push_receipt = PushAtelierCommitReceipt.from_result_bytes(
            bytes(receipts[0].result)
        )
        assert push_receipt.commit_oid == _git(
            remote, "rev-parse", push_receipt.full_ref
        )
        assert push_receipt.parent == base
        assert push_receipt.candidate_tree == _git(
            remote, "rev-parse", f"{push_receipt.commit_oid}^{{tree}}"
        )
        assert push_receipt.author == author
        assert push_receipt.committer == committer

        open_request = OpenPullRequest.from_canonical_bytes(
            final_intents[1].request.payload
        )
        assert open_request.head_branch.value == push_receipt.branch
        assert github.recorded_pull_requests()[0].branch == push_receipt.branch

        with runtime.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.exec_driver_sql("DROP TRIGGER effect_receipts_no_delete")
            connection.execute(
                effect_receipts.delete().where(
                    effect_receipts.c.run_id == RUN.value,
                    effect_receipts.c.operation_name
                    == AdapterOperationName.PUSH_ATELIER_COMMIT.value,
                )
            )
            connection.execute(
                runs.update()
                .where(runs.c.run_id == RUN.value)
                .values(
                    state=RunState.STARTED.value,
                    current_node_id="open-pull-request",
                    terminal_hash=None,
                )
            )
            connection.commit()
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        with (
            runtime.engine.begin() as connection,
            pytest.raises(RunEffectConflict, match="confirmed push receipt"),
        ):
            graph_action_intent(
                connection,
                RUN,
                workflow.revision_hash,
                (github.binding, push.binding),
                PROJECT,
            )
    finally:
        runtime.close()
