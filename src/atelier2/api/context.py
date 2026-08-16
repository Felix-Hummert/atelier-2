from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Depends, FastAPI, Request

from atelier2.api.limits import ApiLimits
from atelier2.api.stream import BoundedQueryRunner, EventPollBackoff
from atelier2.application.answer_wait import AnswerWaitResult
from atelier2.application.prepare_run_events import PrepareRunEventsResult
from atelier2.application.publish_agent_configurations import (
    PublishAgentConfigurationRevisionResult,
    PublishAuthProfileRevisionResult,
)
from atelier2.application.publish_workflow_revision import WorkflowPublicationLimits
from atelier2.application.read_runs import GetRunResult, ListRunsResult
from atelier2.application.read_workflow_revisions import (
    GetWorkflowRevisionResult,
    ListDescribedWorkflowRevisionsResult,
    ListWorkflowRevisionsResult,
)
from atelier2.application.reconcile_effect import ReconcileRunResult
from atelier2.application.reconcile_run import ReconcileRunRequest
from atelier2.application.start_published_run import (
    AuthoredAgentBinding,
    StartPublishedRunResult,
)
from atelier2.contracts.executions import SubmitWaitAnswerRequest
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.ports.agent_attempts import TransactionalAgentAttemptCanceller
from atelier2.ports.agent_configurations import AgentConfigurationCatalog
from atelier2.ports.durable_runs import (
    DurablePublishedRunStarter,
    TransactionalWaitAnswerer,
)
from atelier2.ports.effects import TransactionalEffectReconcileCommander
from atelier2.ports.run_events import RunEventQueries
from atelier2.ports.run_queries import RunQueries
from atelier2.ports.workflow_revisions import (
    WorkflowDocumentParser,
    WorkflowRevisionPublisher,
    WorkflowRevisionQueries,
)


@dataclass(frozen=True)
class ApiPorts:
    workflow_revision_publisher: WorkflowRevisionPublisher
    published_run_starter: DurablePublishedRunStarter
    wait_answerer: TransactionalWaitAnswerer
    reconcile_commander: TransactionalEffectReconcileCommander
    workflow_revision_queries: WorkflowRevisionQueries
    run_queries: RunQueries
    run_event_queries: RunEventQueries
    workflow_document_parser: WorkflowDocumentParser
    agent_configuration_catalog: AgentConfigurationCatalog
    agent_attempt_canceller: TransactionalAgentAttemptCanceller


@dataclass(frozen=True)
class ApiUseCases:
    """The application calls, already bound to their ports by the composition.

    Every field is a call, not a protocol: the port is spent at composition time,
    and what a route holds is the decision, whose result type belongs to
    `atelier2.application`. A field annotated with anything that resolves to
    `atelier2.ports` would hand the port straight back through this record — which
    is the evasion `scripts/check_architecture.py` reads these annotations for.

    The calls stay synchronous because admitting one to the process-wide query
    budget is the API's decision, not the application's: the route runs them
    through its own bounded runner and owns the refusal a full budget produces.
    """

    get_workflow_revision: Callable[[WorkflowRevisionHash], GetWorkflowRevisionResult]
    list_workflow_revisions: Callable[
        [WorkflowRevisionHash | None, int], ListWorkflowRevisionsResult
    ]
    list_described_workflow_revisions: Callable[
        [WorkflowRevisionHash | None, int], ListDescribedWorkflowRevisionsResult
    ]
    get_run: Callable[[RunId], GetRunResult]
    list_runs: Callable[[RunId | None, int], ListRunsResult]
    prepare_run_events: Callable[[RunId, int], PrepareRunEventsResult]
    publish_auth_profile_revision: Callable[
        [str, int, str, str], PublishAuthProfileRevisionResult
    ]
    publish_agent_configuration_revision: Callable[
        [str, str, str, str], PublishAgentConfigurationRevisionResult
    ]
    start_published_run: Callable[
        [RunId, WorkflowRevisionHash, tuple[AuthoredAgentBinding, ...] | None],
        StartPublishedRunResult,
    ]
    answer_wait: Callable[[SubmitWaitAnswerRequest], AnswerWaitResult]
    reconcile_run: Callable[[ReconcileRunRequest], ReconcileRunResult]


@dataclass(frozen=True)
class ApiContext:
    """Everything a route needs that the composition decided once, at startup.

    A route reads it through one dependency instead of closing over the
    variables of `create_app`, which is what makes the route an importable,
    separately callable object rather than a local of a builder function.
    """

    source_commit: str
    source_tree: str
    use_cases: ApiUseCases
    ports: ApiPorts
    limits: ApiLimits
    control_runner: BoundedQueryRunner
    event_runner: BoundedQueryRunner
    workflow_projection_limit: WorkflowPublicationLimits
    event_poll_backoff: EventPollBackoff


def install_api_context(app: FastAPI, context: ApiContext) -> None:
    app.state.api_context = context


async def api_context(request: Request) -> ApiContext:
    """Hand the routes the context the composition installed.

    Declared async on purpose: FastAPI runs a non-coroutine dependency through
    `run_in_threadpool`, which would put a worker-thread hop and a slot of the
    process-wide thread limiter on the request path of every endpoint — for an
    attribute read the routes used to get for free from a closure.
    """

    context: ApiContext = request.app.state.api_context
    return context


api_context_dependency = Depends(api_context)
