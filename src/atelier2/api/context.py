from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, FastAPI, Request

from atelier2.api.limits import ApiLimits
from atelier2.api.stream import BoundedQueryRunner, EventPollBackoff
from atelier2.application.publish_workflow_revision import WorkflowPublicationLimits
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
class ApiContext:
    """Everything a route needs that the composition decided once, at startup.

    A route reads it through one dependency instead of closing over the
    variables of `create_app`, which is what makes the route an importable,
    separately callable object rather than a local of a builder function.
    """

    source_commit: str
    source_tree: str
    ports: ApiPorts
    limits: ApiLimits
    control_runner: BoundedQueryRunner
    event_runner: BoundedQueryRunner
    workflow_projection_limit: WorkflowPublicationLimits
    event_poll_backoff: EventPollBackoff


def install_api_context(app: FastAPI, context: ApiContext) -> None:
    app.state.api_context = context


def api_context(request: Request) -> ApiContext:
    context: ApiContext = request.app.state.api_context
    return context


api_context_dependency = Depends(api_context)
