from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from typing import cast

import pytest
from fastapi.testclient import TestClient

from atelier2.adapters.yaml_workflows import parse_executable_workflow_document
from atelier2.api.app import create_app
from atelier2.api.context import ApiPorts
from atelier2.contracts.effects import (
    AdapterOperationalIdentity,
    AdapterRevision,
    CanonicalRequest,
    EffectBinding,
    EffectDestination,
    EffectIntent,
    EffectIntentSnapshot,
    EffectIntentState,
    EffectIntentStateVersion,
    LogicalEffectKey,
    OperatorAuthoritativeAbsence,
    ReconcileActor,
    ReconcileCommand,
    ReconcileCommandId,
    ReconcileCommandSnapshot,
    ReconcileCommandState,
)
from atelier2.contracts.executions import (
    NodeExecutionId,
    SubmitWaitAnswerRequest,
    WaitAnswer,
    WaitAnswerSnapshot,
    WaitAnswerState,
)
from atelier2.contracts.runs import (
    Run,
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.ports.durable_runs import (
    AnyStartPublishedRunRequest,
    DurableAnswerBytesConflict,
    DurableAnswerCreated,
    DurableAnswerExisting,
    DurableAnswerNodeMissing,
    DurableAnswerResult,
    DurableAnswerRevisionConflict,
    DurableAnswerRunMissing,
    DurableAnswerStateConflict,
    DurablePublishedRunResult,
    DurableRunCreated,
    DurableRunExisting,
    DurableRunFormatNotExecutable,
    DurableRunIdentityConflict,
    DurableRunRevisionMissing,
    DurableStateCorrupt,
    DurableWriteUnavailable,
)
from atelier2.ports.effects import (
    DurableReconciliationCommandConflict,
    DurableReconciliationCreated,
    DurableReconciliationDeterminationConflict,
    DurableReconciliationExisting,
    DurableReconciliationResult,
)
from atelier2.ports.run_events import (
    CursorAhead,
    EventHistoryCorrupt,
    PrepareRunEventStreamResult,
    ReadRunEventPageResult,
    StreamReady,
)
from atelier2.ports.run_queries import (
    GetReconciliationRetryTargetResult,
    GetRunResult,
    ListRunsResult,
    ReconciliationRetryCommandConflict,
    ReconciliationRetryTargetMissing,
    RunFound,
    RunPage,
    RunProjection,
    RunQueryMissing,
    WaitingReconciliationProjection,
)
from atelier2.ports.workflow_revisions import (
    DurableProjectionLimit,
    DurableRevisionCollision,
    DurableRevisionCreated,
    DurableRevisionExisting,
    DurableRevisionPublicationResult,
    GetWorkflowRevisionResult,
    ListWorkflowRevisionsResult,
    QueryDurableStateCorrupt,
    ReadUnavailable,
    WorkflowRevisionFound,
    WorkflowRevisionMissing,
    WorkflowRevisionPage,
    WorkflowRevisionProjection,
)
from tests.scenarios.api import api_limits, api_ports, event_poll_backoff

DOCUMENT = b"""format_version: 1
start: final
nodes:
  - {id: final, type: subworkflow, operation: add, operands: [2, 3], next: null}
"""
REVISION = WorkflowRevision(DOCUMENT)
GRAPH = parse_executable_workflow_document(DOCUMENT)
REVISION_PROJECTION = WorkflowRevisionProjection(REVISION, GRAPH)
RUN = Run(RunId("run"), REVISION.revision_hash, RunState.STARTED, "final", 0, 0)
RUN_PROJECTION = RunProjection(RUN, GRAPH, None)
ANSWER = WaitAnswer(
    RUN.run_id,
    REVISION.revision_hash,
    "wait",
    NodeExecutionId.for_node(RUN.run_id, REVISION.revision_hash, "wait"),
    b"3",
)
PENDING_ANSWER = WaitAnswerSnapshot(ANSWER, WaitAnswerState.PENDING, 0)
APPLIED_ANSWER = WaitAnswerSnapshot(ANSWER, WaitAnswerState.APPLIED, 1)
INTENT = EffectIntent(
    EffectBinding(
        LogicalEffectKey("matrix-effect"),
        RUN.run_id,
        REVISION.revision_hash,
        AdapterRevision("matrix-adapter"),
        EffectDestination("matrix-destination"),
        AdapterOperationalIdentity("matrix-operation"),
    ),
    CanonicalRequest(b"request"),
)
INTENT_SNAPSHOT = EffectIntentSnapshot(
    INTENT,
    EffectIntentState.WAITING_RECONCILIATION,
    EffectIntentStateVersion(1),
)
COMMAND = ReconcileCommand(
    ReconcileCommandId("command"),
    INTENT.reference,
    EffectIntentStateVersion(1),
    ReconcileActor("operator"),
    "inspected",
    OperatorAuthoritativeAbsence(),
)
PENDING_COMMAND = ReconcileCommandSnapshot(COMMAND, ReconcileCommandState.PENDING)
APPLIED_COMMAND = ReconcileCommandSnapshot(COMMAND, ReconcileCommandState.APPLIED)
REJECTED_COMMAND = ReconcileCommandSnapshot(
    COMMAND, ReconcileCommandState.REJECTED_CONFLICT
)
RECONCILIATION_PROJECTION = RunProjection(
    RUN,
    GRAPH,
    WaitingReconciliationProjection(INTENT_SNAPSHOT, None),
)
RUN_BODY = {
    "run_id": "run",
    "public_run_reference": "run1.cnVu",
    "workflow_revision_hash": hashlib.sha256(DOCUMENT).hexdigest(),
    "state_version": 0,
    "state": "STARTED",
    "current_node": {
        "type": "subworkflow",
        "node_id": "final",
        "operation": "add",
        "operands": [2, 3],
        "next_node_id": None,
    },
    "waiting": {"type": "NONE"},
    "terminal_hash": None,
    "latest_event_cursor": None,
}


@dataclass(frozen=True)
class RouteResultCase:
    identifier: str
    operation: str
    source: str
    result: object | None
    status: int
    problem_code: str | None = None


SUCCESS_CASES = (
    ("publish-created", "publish", "publisher", DurableRevisionCreated(REVISION), 201),
    (
        "publish-existing",
        "publish",
        "publisher",
        DurableRevisionExisting(REVISION),
        200,
    ),
    (
        "revision-list-page",
        "revision-list",
        "revision-list",
        WorkflowRevisionPage((), None),
        200,
    ),
    (
        "revision-get-found",
        "revision-get",
        "revision-get",
        WorkflowRevisionFound(REVISION_PROJECTION),
        200,
    ),
    ("start-created", "start", "starter", DurableRunCreated(RUN), 201),
    ("start-existing", "start", "starter", DurableRunExisting(RUN), 200),
    ("run-list-page", "run-list", "run-list", RunPage((), None), 200),
    ("run-get-found", "run-get", "run-get", RunFound(RUN_PROJECTION), 200),
    (
        "wait-created-pending",
        "wait",
        "answerer",
        DurableAnswerCreated(PENDING_ANSWER),
        202,
    ),
    (
        "wait-existing-pending",
        "wait",
        "answerer",
        DurableAnswerExisting(PENDING_ANSWER),
        202,
    ),
    (
        "wait-existing-applied",
        "wait",
        "answerer",
        DurableAnswerExisting(APPLIED_ANSWER),
        200,
    ),
    (
        "reconcile-created-pending",
        "reconcile",
        "commander",
        DurableReconciliationCreated(PENDING_COMMAND),
        202,
    ),
    (
        "reconcile-existing-pending",
        "reconcile",
        "commander",
        DurableReconciliationExisting(PENDING_COMMAND),
        202,
    ),
    (
        "reconcile-existing-applied",
        "reconcile",
        "commander",
        DurableReconciliationExisting(APPLIED_COMMAND),
        200,
    ),
    ("events-ready", "events", "events", StreamReady(0, True, 0), 200),
)
PROBLEM_CASES = (
    (
        "publish-invalid",
        "publish",
        "invalid-publish",
        None,
        422,
        "invalid-workflow-document",
    ),
    (
        "publish-collision",
        "publish",
        "publisher",
        DurableRevisionCollision(),
        409,
        "revision-collision",
    ),
    (
        "publish-unavailable",
        "publish",
        "publisher",
        DurableWriteUnavailable(),
        503,
        "temporarily-unavailable",
    ),
    (
        "publish-corrupt",
        "publish",
        "publisher",
        DurableStateCorrupt(),
        500,
        "durable-state-corrupt",
    ),
    (
        "revision-list-unavailable",
        "revision-list",
        "revision-list",
        ReadUnavailable(),
        503,
        "temporarily-unavailable",
    ),
    (
        "revision-list-corrupt",
        "revision-list",
        "revision-list",
        QueryDurableStateCorrupt(),
        500,
        "durable-state-corrupt",
    ),
    (
        "revision-get-missing",
        "revision-get",
        "revision-get",
        WorkflowRevisionMissing(),
        404,
        "workflow-revision-not-found",
    ),
    (
        "revision-get-unavailable",
        "revision-get",
        "revision-get",
        ReadUnavailable(),
        503,
        "temporarily-unavailable",
    ),
    (
        "revision-get-corrupt",
        "revision-get",
        "revision-get",
        QueryDurableStateCorrupt(),
        500,
        "durable-state-corrupt",
    ),
    (
        "start-revision-missing",
        "start",
        "starter",
        DurableRunRevisionMissing(),
        404,
        "workflow-revision-not-found",
    ),
    (
        "start-identity-conflict",
        "start",
        "starter",
        DurableRunIdentityConflict(),
        409,
        "run-identity-conflict",
    ),
    (
        "start-format-not-executable",
        "start",
        "starter",
        DurableRunFormatNotExecutable(),
        409,
        "workflow-format-not-executable",
    ),
    (
        "start-unavailable",
        "start",
        "starter",
        DurableWriteUnavailable(),
        503,
        "temporarily-unavailable",
    ),
    (
        "start-corrupt",
        "start",
        "starter",
        DurableStateCorrupt(),
        500,
        "durable-state-corrupt",
    ),
    (
        "run-list-unavailable",
        "run-list",
        "run-list",
        ReadUnavailable(),
        503,
        "temporarily-unavailable",
    ),
    (
        "run-list-corrupt",
        "run-list",
        "run-list",
        QueryDurableStateCorrupt(),
        500,
        "durable-state-corrupt",
    ),
    ("run-get-missing", "run-get", "run-get", RunQueryMissing(), 404, "run-not-found"),
    (
        "run-get-unavailable",
        "run-get",
        "run-get",
        ReadUnavailable(),
        503,
        "temporarily-unavailable",
    ),
    (
        "run-get-corrupt",
        "run-get",
        "run-get",
        QueryDurableStateCorrupt(),
        500,
        "durable-state-corrupt",
    ),
    (
        "wait-run-missing",
        "wait",
        "answerer",
        DurableAnswerRunMissing(),
        404,
        "run-not-found",
    ),
    (
        "wait-node-missing",
        "wait",
        "answerer",
        DurableAnswerNodeMissing(),
        404,
        "node-not-found",
    ),
    (
        "wait-revision-conflict",
        "wait",
        "answerer",
        DurableAnswerRevisionConflict(),
        409,
        "answer-revision-conflict",
    ),
    (
        "wait-state-conflict",
        "wait",
        "answerer",
        DurableAnswerStateConflict(),
        409,
        "answer-state-conflict",
    ),
    (
        "wait-bytes-conflict",
        "wait",
        "answerer",
        DurableAnswerBytesConflict(),
        409,
        "answer-bytes-conflict",
    ),
    (
        "wait-unavailable",
        "wait",
        "answerer",
        DurableWriteUnavailable(),
        503,
        "temporarily-unavailable",
    ),
    (
        "wait-corrupt",
        "wait",
        "answerer",
        DurableStateCorrupt(),
        500,
        "durable-state-corrupt",
    ),
    (
        "reconcile-run-missing",
        "reconcile",
        "reconcile-run",
        RunQueryMissing(),
        404,
        "run-not-found",
    ),
    (
        "reconcile-target-missing",
        "reconcile",
        "reconcile-retry",
        ReconciliationRetryTargetMissing(),
        409,
        "reconciliation-target-missing",
    ),
    (
        "reconcile-retry-command-conflict",
        "reconcile",
        "reconcile-retry",
        ReconciliationRetryCommandConflict(),
        409,
        "reconciliation-command-conflict",
    ),
    (
        "reconcile-retry-run-missing",
        "reconcile",
        "reconcile-retry",
        RunQueryMissing(),
        404,
        "run-not-found",
    ),
    (
        "reconcile-retry-unavailable",
        "reconcile",
        "reconcile-retry",
        ReadUnavailable(),
        503,
        "temporarily-unavailable",
    ),
    (
        "reconcile-retry-corrupt",
        "reconcile",
        "reconcile-retry",
        QueryDurableStateCorrupt(),
        500,
        "durable-state-corrupt",
    ),
    (
        "reconcile-stale",
        "reconcile",
        "commander",
        DurableReconciliationCreated(REJECTED_COMMAND),
        409,
        "reconciliation-stale",
    ),
    (
        "reconcile-command-conflict",
        "reconcile",
        "commander",
        DurableReconciliationCommandConflict(),
        409,
        "reconciliation-command-conflict",
    ),
    (
        "reconcile-determination-conflict",
        "reconcile",
        "commander",
        DurableReconciliationDeterminationConflict(),
        409,
        "reconciliation-determination-conflict",
    ),
    (
        "reconcile-existing-rejected",
        "reconcile",
        "commander",
        DurableReconciliationExisting(REJECTED_COMMAND),
        409,
        "reconciliation-rejected",
    ),
    (
        "reconcile-unavailable",
        "reconcile",
        "commander",
        DurableWriteUnavailable(),
        503,
        "temporarily-unavailable",
    ),
    (
        "reconcile-corrupt",
        "reconcile",
        "commander",
        DurableStateCorrupt(),
        500,
        "durable-state-corrupt",
    ),
    ("events-run-missing", "events", "events", RunQueryMissing(), 404, "run-not-found"),
    (
        "events-cursor-ahead",
        "events",
        "events",
        CursorAhead(),
        409,
        "event-cursor-ahead",
    ),
    (
        "events-history-corrupt",
        "events",
        "events",
        EventHistoryCorrupt(),
        500,
        "durable-state-corrupt",
    ),
    (
        "events-query-corrupt",
        "events",
        "events",
        QueryDurableStateCorrupt(),
        500,
        "durable-state-corrupt",
    ),
    (
        "events-unavailable",
        "events",
        "events",
        ReadUnavailable(),
        503,
        "temporarily-unavailable",
    ),
)
CASES = tuple(RouteResultCase(*values) for values in SUCCESS_CASES) + tuple(
    RouteResultCase(*values) for values in PROBLEM_CASES
)


@dataclass
class MatrixPublisher:
    case: RouteResultCase

    def publish(self, revision: WorkflowRevision) -> DurableRevisionPublicationResult:
        del revision
        if self.case.source == "invalid-publish":
            raise AssertionError("invalid workflow reached the publisher")
        assert self.case.source == "publisher"
        return cast(DurableRevisionPublicationResult, self.case.result)


@dataclass
class MatrixStarter:
    case: RouteResultCase

    def start_published(
        self, request: AnyStartPublishedRunRequest
    ) -> DurablePublishedRunResult:
        del request
        assert self.case.source == "starter"
        return cast(DurablePublishedRunResult, self.case.result)


@dataclass
class MatrixAnswerer:
    case: RouteResultCase

    def submit_result(self, request: SubmitWaitAnswerRequest) -> DurableAnswerResult:
        del request
        assert self.case.source == "answerer"
        return cast(DurableAnswerResult, self.case.result)


@dataclass
class MatrixCommander:
    case: RouteResultCase

    def submit_result(self, command: ReconcileCommand) -> DurableReconciliationResult:
        del command
        assert self.case.source == "commander"
        return cast(DurableReconciliationResult, self.case.result)


@dataclass
class MatrixQueries:
    case: RouteResultCase
    run_reads: int = 0

    def list_workflow_revisions(
        self, after: WorkflowRevisionHash | None, limit: int
    ) -> ListWorkflowRevisionsResult:
        del after, limit
        assert self.case.source == "revision-list"
        return cast(ListWorkflowRevisionsResult, self.case.result)

    def get_workflow_revision(
        self,
        revision_hash: WorkflowRevisionHash,
        projection_limit: DurableProjectionLimit | None = None,
    ) -> GetWorkflowRevisionResult:
        del revision_hash, projection_limit
        assert self.case.source == "revision-get"
        return cast(GetWorkflowRevisionResult, self.case.result)

    def list_runs(
        self,
        after: RunId | None,
        limit: int,
        projection_limit: DurableProjectionLimit | None = None,
    ) -> ListRunsResult:
        del after, limit, projection_limit
        assert self.case.source == "run-list"
        return cast(ListRunsResult, self.case.result)

    def get_run(
        self,
        run_id: RunId,
        projection_limit: DurableProjectionLimit | None = None,
    ) -> GetRunResult:
        del run_id, projection_limit
        self.run_reads += 1
        if self.case.source == "run-get":
            return cast(GetRunResult, self.case.result)
        if self.case.source == "reconcile-run":
            return cast(GetRunResult, self.case.result)
        if self.case.source == "reconcile-retry":
            return RunFound(RUN_PROJECTION)
        if self.case.source == "events":
            return RunFound(RUN_PROJECTION)
        if self.case.source == "commander" and self.run_reads == 1:
            return RunFound(RECONCILIATION_PROJECTION)
        if self.case.source in {"starter", "answerer", "commander"}:
            return RunFound(RUN_PROJECTION)
        raise AssertionError("matrix route unexpectedly read a run")

    def get_reconciliation_retry_target(
        self,
        run_id: RunId,
        command_id: ReconcileCommandId,
        projection_limit: DurableProjectionLimit | None = None,
    ) -> GetReconciliationRetryTargetResult:
        del run_id, command_id, projection_limit
        assert self.case.source == "reconcile-retry"
        return cast(GetReconciliationRetryTargetResult, self.case.result)

    def prepare_run_event_stream(
        self, run_id: RunId, after_sequence: int
    ) -> PrepareRunEventStreamResult:
        del run_id, after_sequence
        assert self.case.source == "events"
        return cast(PrepareRunEventStreamResult, self.case.result)

    def read_run_event_page(
        self,
        run_id: RunId,
        after_sequence: int,
        limit: int,
        projection_limit: DurableProjectionLimit | None = None,
    ) -> ReadRunEventPageResult:
        del run_id, after_sequence, limit, projection_limit
        raise AssertionError("terminal matrix stream unexpectedly paged events")


def _ports(case: RouteResultCase) -> ApiPorts:
    queries = MatrixQueries(case)
    return api_ports(
        workflow_revision_publisher=MatrixPublisher(case),
        published_run_starter=MatrixStarter(case),
        wait_answerer=MatrixAnswerer(case),
        reconcile_commander=MatrixCommander(case),
        workflow_revision_queries=queries,
        run_queries=queries,
        run_event_queries=queries,
        workflow_document_parser=parse_executable_workflow_document,
        agent_configuration_catalog=queries,
    )


def _request(client: TestClient, case: RouteResultCase):
    if case.operation == "publish":
        document = b"not a workflow" if case.source == "invalid-publish" else DOCUMENT
        return client.post(
            "/atelier/api/v1/workflow-revisions",
            content=document,
            headers={"content-type": "application/yaml"},
        )
    if case.operation == "revision-list":
        return client.get("/atelier/api/v1/workflow-revisions")
    if case.operation == "revision-get":
        return client.get(
            "/atelier/api/v1/workflow-revisions/" + REVISION.revision_hash.value
        )
    if case.operation == "start":
        return client.post(
            "/atelier/api/v1/runs",
            json={
                "run_id": "run",
                "workflow_revision_hash": REVISION.revision_hash.value,
            },
        )
    if case.operation == "run-list":
        return client.get("/atelier/api/v1/runs")
    if case.operation == "run-get":
        return client.get("/atelier/api/v1/runs/run1.cnVu")
    if case.operation == "wait":
        return client.post(
            "/atelier/api/v1/runs/run1.cnVu/answers",
            json={
                "revision_hash": REVISION.revision_hash.value,
                "node_id": "wait",
                "answer_base64": "Mw==",
            },
        )
    if case.operation == "reconcile":
        return client.post(
            "/atelier/api/v1/runs/run1.cnVu/reconciliations",
            json={
                "command_id": "command",
                "expected_intent_state_version": 1,
                "actor": "operator",
                "evidence": "inspected",
                "determination": {"type": "operator_authoritative_absence"},
            },
        )
    if case.operation == "events":
        return client.get(
            "/atelier/api/v1/runs/run1.cnVu/events",
            headers={"accept": "text/event-stream"},
        )
    raise AssertionError(f"matrix has no request for {case.operation}")


def _success_body(operation: str) -> object:
    revision_body = {
        "revision_hash": REVISION.revision_hash.value,
        "document_base64": base64.b64encode(DOCUMENT).decode("ascii"),
        "graph": {
            "format_version": 1,
            "start_node_id": "final",
            "nodes": [
                {
                    "type": "subworkflow",
                    "node_id": "final",
                    "operation": "add",
                    "operands": [2, 3],
                    "next_node_id": None,
                }
            ],
        },
    }
    if operation in {"publish", "revision-get"}:
        return revision_body
    if operation == "revision-list":
        return {"items": [], "next_after_revision_hash": None}
    if operation in {"start", "run-get", "wait", "reconcile"}:
        return RUN_BODY
    if operation == "run-list":
        return {"items": [], "next_after": None}
    if operation == "events":
        return b""
    raise AssertionError(f"matrix has no body oracle for {operation}")


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.identifier)
def test_every_application_result_branch_has_exact_http_mapping(
    case: RouteResultCase,
) -> None:
    client = TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=_ports(case),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        ),
        raise_server_exceptions=False,
    )

    response = _request(client, case)

    assert response.status_code == case.status
    if case.problem_code is not None:
        assert response.headers["content-type"] == "application/problem+json"
        assert response.json()["type"] == (
            "urn:atelier2:problem:v1:" + case.problem_code
        )
        return
    if case.operation == "events":
        assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
        assert response.content == _success_body(case.operation)
    else:
        assert response.headers["content-type"] == "application/json"
        assert response.json() == _success_body(case.operation)


def test_reconciliation_retry_preserves_durable_projection_limit_detail() -> None:
    case = RouteResultCase(
        "reconcile-retry-projection-limit",
        "reconcile",
        "reconcile-retry",
        ReadUnavailable("Durable projection exceeds configured API limits."),
        503,
        "temporarily-unavailable",
    )
    client = TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=_ports(case),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        )
    )

    response = _request(client, case)

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "Durable projection exceeds configured API limits."
    )
