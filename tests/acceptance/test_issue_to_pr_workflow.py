"""The repository's issue-to-pr workflow runs from one issue order to one PR."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from httpx import Response

from atelier2.adapters.candidate_store import CANDIDATE_STORE_DIRECTORY_NAME
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.effect_store import intent_snapshot_from_record
from atelier2.adapters.dbos.run_store import DbosWaitAnswerer
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import (
    agent_receipts_v2,
    effect_intents,
    effect_receipts,
    runs,
    tool_redemptions,
)
from atelier2.adapters.dbos.starter import DbosWorkflowRevisionPublisher
from atelier2.adapters.git_transport.effects import (
    GitRemote,
    GitTransportEffectAdapterFactory,
)
from atelier2.adapters.github.effects import GitHubEffectAdapterFactory
from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.api.openapi import API_PREFIX
from atelier2.application.answer_wait import UnanswerableWait, answer_wait_result
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
from atelier2.contracts.effect_markers import body_carries_request_hash
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
from atelier2.contracts.executions import (
    NodeExecutionId,
    SubmitWaitAnswerRequest,
    WaitAnswerActor,
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
    emitting,
    launching,
    publish_checked_model_registry,
)
from tests.scenarios.api import durable_api_client
from tests.scenarios.issue_observation import FakeTrackerItemSource
from tests.scenarios.projects import declaring_verification, git_project
from tests.scenarios.run_waiting import wait_for_run_state
from tests.scenarios.runs import submit_reconcile_command, submit_wait_answer

WORKFLOW_PATH = Path("workflows/issue-to-pr.yaml")
BUDGET_PATH = Path("workflows/budgets/push-implement.json")
PROJECT = ProjectId("issue-to-pr-workflow")
ITEM = TrackerItemReference("gh:1123")
RUN = RunId("v3/issue-to-pr")
ORDER_NAME = "context"
BUILD_NODE = "build"
REVIEW_NODE = "review"
WAIT_NODE = "authorize_pr"
BUILDER_PROVIDER = ProviderId("exact")
REVIEWER_PROVIDER = ProviderId("other")
"""Two provider families, because the document refuses to cast them as one.

`family_differs_from: builder` is resolved against the provider each bound
configuration names, so a chain whose reviewer shares the builder's provider is
refused at the start rather than reviewed by the builder's own family.
"""

CANDIDATE_FILE_NAME = "candidate.txt"
CANDIDATE_FILE_BYTES = b"what the builder changed\n"
CANDIDATE_REPORT = json.dumps(
    {
        "summary": "Wrote the line the item asked for.",
        "diff": f"--- /dev/null\n+++ b/{CANDIDATE_FILE_NAME}\n+what the builder changed\n",
    }
).encode()
REVIEW_RESULT = json.dumps({"findings": [], "verdict": "approve"}).encode()
"""The reviewer's own bytes, deliberately unlike the builder's.

The pull request body must be traceable to the builder's report specifically;
identical bytes would let a body composed from whichever agent ran last pass.
"""
RELEASE_ANSWER = b'"open-pr"'
REFUSED_ANSWER = b'"cancel"'
VERIFICATION_OUTPUT = b"green"

_EDIT_THEN_REPORT = (
    "import os,pathlib,sys;"
    "pathlib.Path(sys.argv[1]).write_bytes(bytes.fromhex(sys.argv[2]));"
    "os.write(1,bytes.fromhex(sys.argv[3]))"
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


def _project_and_remote(root: Path, verification_record: Path) -> tuple[Path, Path]:
    """A project that states how it is verified, and the remote its push reaches.

    The declared command reads the file the builder writes into its lease, so
    what the record holds afterwards proves the check ran on the changed tree
    rather than on the pinned one.
    """
    project = root / "project"
    git_project(
        project,
        declaring_verification(
            [
                "/bin/sh",
                "-c",
                (
                    f"cat {CANDIDATE_FILE_NAME} > {verification_record}; "
                    f"printf '{VERIFICATION_OUTPUT.decode('ascii')}'"
                ),
            ]
        ),
    )
    remote = root / "remote.git"
    _git(root, "init", "--bare", "--quiet", str(remote))
    _git(project, "push", "--quiet", str(remote), "HEAD:refs/heads/main")
    return project, remote


def _executors() -> tuple[RecordingAgentExecutorFactoryV2, ...]:
    """One provider that edits its lease and reports it, one that only judges."""
    return (
        RecordingAgentExecutorFactoryV2(
            BUILDER_PROVIDER.value,
            f"{BUILDER_PROVIDER.value}/v1",
            f"{BUILDER_PROVIDER.value}-operation",
            b"",
            capability_set=frozenset({AgentExecutionCapability.HEADLESS_WITH_TOOLS}),
            command=launching(
                sys.executable,
                "-c",
                _EDIT_THEN_REPORT,
                CANDIDATE_FILE_NAME,
                CANDIDATE_FILE_BYTES.hex(),
                CANDIDATE_REPORT.hex(),
            ),
        ),
        RecordingAgentExecutorFactoryV2(
            REVIEWER_PROVIDER.value,
            f"{REVIEWER_PROVIDER.value}/v1",
            f"{REVIEWER_PROVIDER.value}-operation",
            b"",
            command=emitting(REVIEW_RESULT),
        ),
    )


def _publish_workflow(
    runtime: DbosRuntime,
) -> tuple[
    WorkflowRevision,
    AgentBindingSet,
    tuple[GitCommitIdentity, GitCommitIdentity],
]:
    # The live revisions pin the operator as author and the pushing node's model
    # as committer (#883, operator ruling 30.08.2026); the shipped document pins
    # that same model, so reproducing this exact pair is what makes the derived
    # grant hash equal the one it names.
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
    verification_grant = PublishedRevision(
        RevisionKind.TOOL,
        json.dumps(
            {"capability": ToolGrantCapability.RUN_PROJECT_VERIFICATION.value},
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
    )
    store = DbosCatalogStore(runtime.engine)
    for revision in (
        PublishedRevision(RevisionKind.SCHEMA, WORK_ITEM_ORDER_SCHEMA_DOCUMENT),
        PublishedRevision(
            RevisionKind.SCHEMA,
            Path("workflows/schemas/issue_to_pr_candidate_report.json").read_bytes(),
        ),
        PublishedRevision(
            RevisionKind.SCHEMA,
            Path("workflows/schemas/code_review_result.json").read_bytes(),
        ),
        PublishedRevision(
            RevisionKind.SCHEMA,
            Path("workflows/schemas/issue_to_pr_release_decision.json").read_bytes(),
        ),
        PublishedRevision(RevisionKind.BUDGET_POLICY, BUDGET_PATH.read_bytes()),
        push_operation,
        PublishedRevision(RevisionKind.ADAPTER_OPERATION, b'{"operation":"open-pr"}'),
        push_grant,
        verification_grant,
    ):
        published = store.publish_revision(revision)
        assert isinstance(
            published, (PublishedRevisionCreated, PublishedRevisionExisting)
        ), published

    shipped_document = WORKFLOW_PATH.read_bytes()
    assert push_grant.revision_hash.value.encode() in shipped_document
    assert verification_grant.revision_hash.value.encode() in shipped_document
    builder = parse_workflow_document(shipped_document).node(BUILD_NODE)
    assert isinstance(builder, AgentNodeV3)
    assert builder.model == pushing_model
    workflow = WorkflowRevision(shipped_document)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)

    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    bindings: list[AgentBinding] = []
    for role, provider, capability in (
        ("builder", BUILDER_PROVIDER, AgentExecutionCapability.HEADLESS_WITH_TOOLS),
        ("reviewer", REVIEWER_PROVIDER, AgentExecutionCapability.HEADLESS),
    ):
        auth = AuthProfileRevision(
            f"{role}-profile", 1, provider, AuthMode.SUBSCRIPTION
        )
        assert isinstance(
            catalog.publish_auth_profile_revision(auth), AuthProfileRevisionCreated
        )
        configuration = AgentConfigurationRevision(
            role,
            auth.revision_hash,
            AgentExecutorRevision(f"{provider.value}/v1"),
            capability,
            AgentConfigurationRevisionFormatVersion.V2,
        )
        assert isinstance(
            catalog.publish_agent_configuration_revision(configuration),
            AgentConfigurationRevisionCreated,
        )
        publish_checked_model_registry(runtime.engine, provider, (configuration,))
        bindings.append(AgentBinding(AgentRole(role), configuration.revision_hash))
    return workflow, AgentBindingSet(tuple(bindings)), (author, committer)


def _start(
    runtime: DbosRuntime, workflow: WorkflowRevision, bindings: AgentBindingSet
) -> Response:
    """Start the run the way the head does: the bindings, and the issue alone."""
    item = ObservedWorkItemRevision(
        ITEM,
        WorkItemKind.ISSUE,
        b"Write the line this run is for.",
        WorkItemChangeMarker("issue-1123-v1"),
        RecordedAt("2026-09-04T12:00:00Z"),
    )
    client = durable_api_client(
        runtime,
        served_project_id=PROJECT,
        tracker_item_source=FakeTrackerItemSource(
            snapshot_answer=WorkItemRevisionObserved(item),
            expected_snapshot_reference=item.item,
        ),
    )
    return client.post(
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
                for binding in bindings.bindings
            ],
            "orders": [{"name": ORDER_NAME, "work_item": ITEM.value}],
        },
    )


@pytest.mark.proves("issue-to-pr-builds-reviews-waits-and-opens-the-pull-request")
def test_issue_to_pr_builds_reviews_waits_then_opens_the_pull_request(
    tmp_path: Path,
) -> None:
    """The whole shipped chain from one work-item order, driven with fake agents.

    The order carries nothing but the issue reference: the builder's brief, the
    reviewer's contract and the branch the push derives all come from that one
    observed item. The commit exists before the review, because the push is the
    builder attempt's own continuation; what the Wait releases is the pull
    request, and only the exact release opens it.
    """
    verification_record = tmp_path / "verification.txt"
    project, remote = _project_and_remote(tmp_path, verification_record)
    github = GitHubEffectAdapterFactory(
        tmp_path / "github.sqlite",
        AdapterRevision("github-open-pr-v1"),
        EffectDestination("platform"),
    )
    push = GitTransportEffectAdapterFactory(
        tmp_path / CANDIDATE_STORE_DIRECTORY_NAME,
        GitRemote("local-issue-to-pr-test", str(remote)),
        AdapterRevision("git-push-v1"),
        EffectDestination("git"),
    )
    runtime = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "issue-to-pr-workflow-test",
            agent_scratch_root=agent_scratch_root(tmp_path),
            project_id=PROJECT,
            bootstrap_project_root=project,
        ),
        EffectAdapterRegistry(
            (
                EffectAdapterRegistration(AdapterOperationName.OPEN_PR, github),
                EffectAdapterRegistration(
                    AdapterOperationName.PUSH_ATELIER_COMMIT, push
                ),
            )
        ),
        _executors(),
    )
    runtime.initialize_storage()
    try:
        workflow, bindings, (author, committer) = _publish_workflow(runtime)
        response = _start(runtime, workflow, bindings)
        assert response.status_code == 201, response.text
        runtime.launch()

        wait_for_run_state(runtime.engine, RUN, RunState.WAITING_RECONCILIATION)
        with runtime.engine.connect() as connection:
            push_intent = intent_snapshot_from_record(
                connection.execute(sa.select(effect_intents)).mappings().one()
            ).intent
            current_node = connection.scalar(
                sa.select(runs.c.current_node_id).where(runs.c.run_id == RUN.value)
            )
            redemption = (
                connection.execute(sa.select(tool_redemptions)).mappings().one()
            )
        assert current_node == BUILD_NODE
        assert push_intent.binding.operation_name is (
            AdapterOperationName.PUSH_ATELIER_COMMIT
        )
        assert str(redemption["node_id"]) == BUILD_NODE
        assert str(redemption["capability"]) == (
            ToolGrantCapability.RUN_PROJECT_VERIFICATION.value
        )
        assert int(redemption["exit_code"]) == 0
        assert verification_record.read_bytes() == CANDIDATE_FILE_BYTES
        assert github.recorded_pull_requests() == ()

        submit_reconcile_command(
            runtime.engine,
            runtime.settings,
            ReconcileCommand(
                ReconcileCommandId("authorize-issue-to-pr-push"),
                push_intent.reference,
                EffectIntentStateVersion(1),
                ReconcileActor("operator"),
                "confirmed the derived branch is absent on the connected remote",
                OperatorAuthoritativeAbsence(),
            ),
        )
        wait_for_run_state(runtime.engine, RUN, RunState.WAITING_INPUT)

        with runtime.engine.connect() as connection:
            reviewed = (
                connection.execute(
                    sa.select(agent_receipts_v2.c.output_bytes).where(
                        agent_receipts_v2.c.run_id == RUN.value,
                        agent_receipts_v2.c.node_id == REVIEW_NODE,
                    )
                )
                .scalars()
                .one()
            )
            waiting_node = connection.scalar(
                sa.select(runs.c.current_node_id).where(runs.c.run_id == RUN.value)
            )
        assert bytes(reviewed) == REVIEW_RESULT
        assert waiting_node == WAIT_NODE
        assert github.recorded_pull_requests() == ()

        wait_execution = NodeExecutionId.for_node(
            RUN, workflow.revision_hash, WAIT_NODE
        )
        refused = answer_wait_result(
            RUN,
            workflow.revision_hash,
            WAIT_NODE,
            wait_execution,
            WaitAnswerActor.OPERATOR,
            REFUSED_ANSWER,
            DbosWaitAnswerer(runtime.engine, runtime.settings.application_version),
        )
        assert isinstance(refused, UnanswerableWait), refused
        wait_for_run_state(runtime.engine, RUN, RunState.WAITING_INPUT)
        assert github.recorded_pull_requests() == ()

        submit_wait_answer(
            runtime.engine,
            runtime.settings.application_version,
            SubmitWaitAnswerRequest(
                RUN,
                workflow.revision_hash,
                WAIT_NODE,
                wait_execution,
                WaitAnswerActor.OPERATOR,
                RELEASE_ANSWER,
            ),
        )
        wait_for_run_state(runtime.engine, RUN, RunState.COMPLETED)

        with runtime.engine.connect() as connection:
            intents = tuple(
                intent_snapshot_from_record(row).intent
                for row in connection.execute(
                    sa.select(effect_intents).order_by(sa.literal_column("rowid"))
                ).mappings()
            )
            receipts = connection.execute(
                sa.select(
                    effect_receipts.c.operation_name, effect_receipts.c.result
                ).order_by(sa.literal_column("rowid"))
            ).all()
        assert [intent.binding.operation_name for intent in intents] == [
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
        assert push_receipt.author == author
        assert push_receipt.committer == committer
        assert push_receipt.commit_oid == _git(
            remote, "rev-parse", push_receipt.full_ref
        )
        opened = OpenPullRequest.from_canonical_bytes(intents[1].request.payload)
        assert opened.head_branch.value == push_receipt.branch

        (recorded,) = github.recorded_pull_requests()
        assert recorded.branch == push_receipt.branch
        assert body_carries_request_hash(
            recorded.body, intents[1].request.request_hash.value
        )
        assert CANDIDATE_REPORT.decode("utf-8") in recorded.body
        assert REVIEW_RESULT.decode("utf-8") not in recorded.body
    finally:
        runtime.close()
