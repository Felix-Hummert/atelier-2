from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Depends, FastAPI, Request

from atelier2.api.limits import ApiLimits
from atelier2.api.stream import BoundedQueryRunner, EventPollBackoff
from atelier2.application.admit_catalog_member import (
    AdmitMemberResult,
    FoundLineageResult,
)
from atelier2.application.admit_queue_item import (
    AdmitQueueItemOutcome,
    ListAdmittedQueueItemsOutcome,
)
from atelier2.application.answer_wait import AnswerWaitResult
from atelier2.application.cancel_agent_attempt import CancelAgentAttemptResult
from atelier2.application.cancel_run import CancelRunResult
from atelier2.application.classify_definition_document import (
    ClassifyDefinitionDocumentResult,
)
from atelier2.application.import_project_source_issues import (
    ImportProjectSourceIssuesOutcome,
    ListObservedQueueItemsOutcome,
)
from atelier2.application.occupancy import (
    GetOccupancyResult,
    PublishOccupancyUseCaseResult,
)
from atelier2.application.prepare_run_events import PrepareRunEventsResult
from atelier2.application.project_root import (
    GetProjectRootResult,
    PublishProjectRootUseCaseResult,
)
from atelier2.application.publish_adapter_operation_revision import (
    PublishAdapterOperationRevisionResult,
)
from atelier2.application.publish_agent_configurations import (
    PublishAgentConfigurationRevisionResult,
    PublishAuthProfileRevisionResult,
)
from atelier2.application.publish_agent_definition_revision import (
    PublishAgentDefinitionRevisionResult,
)
from atelier2.application.publish_artifact import PublishArtifactUseCaseResult
from atelier2.application.publish_budget_revision import PublishBudgetRevisionResult
from atelier2.application.publish_schema_revision import (
    GetSchemaRevisionResult,
    PublishSchemaRevisionResult,
)
from atelier2.application.publish_tool_grant_revision import (
    PublishToolGrantRevisionResult,
)
from atelier2.application.publish_workflow_revision import (
    PublishWorkflowRevisionResult,
    WorkflowPublicationLimits,
)
from atelier2.application.read_agent_configurations import (
    ListAgentConfigurationRevisionsResult,
    ListAuthProfileRevisionsResult,
)
from atelier2.application.read_agent_definition_revisions import (
    ListAgentDefinitionRevisionsResult,
)
from atelier2.application.read_attention_events import ReadAttentionEventsResult
from atelier2.application.read_projects import (
    GetProjectResult,
    ListProjectsResult,
)
from atelier2.application.read_run_events import ReadRunEventsResult
from atelier2.application.read_runs import (
    GetNodeDetailUseCaseResult,
    GetRunResult,
    ListRunReceiptsUseCaseResult,
    ListRunsResult,
)
from atelier2.application.read_workflow_revisions import (
    GetWorkflowRevisionResult,
    ListDescribedWorkflowRevisionsResult,
    ListWorkflowRevisionsResult,
)
from atelier2.application.reconcile_effect import ReconcileRunResult
from atelier2.application.reconcile_run import ReconcileRunRequest
from atelier2.application.reconstruct_agent_definition import (
    AgentDefinitionParser,
    AgentDefinitionRenderer,
)
from atelier2.application.resolve_catalog_name import CatalogNameResult
from atelier2.application.start_published_run import (
    AuthoredAgentBinding,
    AuthoredOrder,
    StartPublishedRunResult,
)
from atelier2.contracts.agent_attempts import CancelAgentAttemptRequest
from atelier2.contracts.agents import (
    AgentConfigurationRevisionHash,
    AuthProfileRevisionHash,
)
from atelier2.contracts.catalog_v3 import (
    CatalogActivatedAt,
    CatalogActor,
    CatalogLineageDisplayName,
    CatalogLineageId,
    CatalogLineageQuery,
)
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.queue_projection import AdmitQueueItem, QueueItemId
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.contracts.runs import RunId, RunState, WorkflowRevisionHash
from atelier2.ports.agent_attempts import TransactionalAgentAttemptCanceller
from atelier2.ports.agent_configurations import AgentConfigurationCatalog
from atelier2.ports.artifacts import ArtifactPublisher
from atelier2.ports.durable_runs import (
    DurablePublishedRunStarter,
    TransactionalWaitAnswerer,
)
from atelier2.ports.effects import TransactionalEffectReconcileCommander
from atelier2.ports.host_configuration import HostConfigurationChannel
from atelier2.ports.issue_observation import TrackerItemSource
from atelier2.ports.published_revisions import (
    CatalogAdmissions,
    CatalogResolver,
    PublishedRevisionListing,
    PublishedRevisionRegistry,
)
from atelier2.ports.queue_projection import QueueProjection
from atelier2.ports.run_events import (
    RunEventQueries,
)
from atelier2.ports.run_queries import (
    RunQueries,
)
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
    agent_definition_parser: AgentDefinitionParser
    agent_definition_renderer: AgentDefinitionRenderer
    agent_configuration_catalog: AgentConfigurationCatalog
    agent_attempt_canceller: TransactionalAgentAttemptCanceller
    catalog_resolver: CatalogResolver
    catalog_admissions: CatalogAdmissions
    published_revision_registry: PublishedRevisionRegistry
    published_revision_listing: PublishedRevisionListing
    artifact_publisher: ArtifactPublisher
    host_configuration_channel: HostConfigurationChannel
    queue_projection: QueueProjection
    # None is the honest default: a composition that serves no connected
    # project has no tracker to observe, and the import door says so by name.
    tracker_item_source: TrackerItemSource | None = None


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
    get_node_detail: Callable[[RunId, str], GetNodeDetailUseCaseResult]
    list_run_receipts: Callable[[RunId], ListRunReceiptsUseCaseResult]
    list_runs: Callable[[RunId | None, int, RunState | None], ListRunsResult]
    prepare_run_events: Callable[[RunId, int], PrepareRunEventsResult]
    read_run_events: Callable[[RunId, int, int], ReadRunEventsResult]
    read_attention_events: Callable[
        [RunId | None, int | None, int, tuple[tuple[RunId, int], ...]],
        ReadAttentionEventsResult,
    ]
    publish_workflow_revision: Callable[[bytes], PublishWorkflowRevisionResult]
    publish_artifact: Callable[[bytes], PublishArtifactUseCaseResult]
    publish_schema_revision: Callable[[bytes], PublishSchemaRevisionResult]
    get_schema_revision: Callable[[PublishedRevisionHash], GetSchemaRevisionResult]
    publish_budget_revision: Callable[[bytes], PublishBudgetRevisionResult]
    publish_tool_grant_revision: Callable[[bytes], PublishToolGrantRevisionResult]
    publish_adapter_operation_revision: Callable[
        [bytes], PublishAdapterOperationRevisionResult
    ]
    publish_agent_definition_revision: Callable[
        [bytes], PublishAgentDefinitionRevisionResult
    ]
    classify_definition_document: Callable[
        [bytes, str | None], ClassifyDefinitionDocumentResult
    ]
    list_agent_definition_revisions: Callable[
        [PublishedRevisionHash | None, int], ListAgentDefinitionRevisionsResult
    ]
    publish_auth_profile_revision: Callable[
        [str, int, str, str], PublishAuthProfileRevisionResult
    ]
    publish_agent_configuration_revision: Callable[
        [str, str, str, str], PublishAgentConfigurationRevisionResult
    ]
    list_agent_configuration_revisions: Callable[
        [AgentConfigurationRevisionHash | None, int],
        ListAgentConfigurationRevisionsResult,
    ]
    list_auth_profile_revisions: Callable[
        [AuthProfileRevisionHash | None, int],
        ListAuthProfileRevisionsResult,
    ]
    list_projects: Callable[[], ListProjectsResult]
    get_project: Callable[[ProjectId], GetProjectResult]
    start_published_run: Callable[
        [
            RunId,
            WorkflowRevisionHash,
            tuple[AuthoredAgentBinding, ...] | None,
            tuple[AuthoredOrder, ...],
        ],
        StartPublishedRunResult,
    ]
    answer_wait: Callable[[RunId, WorkflowRevisionHash, str, bytes], AnswerWaitResult]
    reconcile_run: Callable[[ReconcileRunRequest], ReconcileRunResult]
    cancel_agent_attempt: Callable[
        [CancelAgentAttemptRequest], CancelAgentAttemptResult
    ]
    cancel_run: Callable[[RunId, str, NodeExecutionId], CancelRunResult]
    resolve_catalog_name: Callable[
        [RevisionKind, CatalogLineageQuery, object], CatalogNameResult
    ]
    found_catalog_lineage: Callable[
        [
            RevisionKind,
            PublishedRevisionHash,
            CatalogLineageDisplayName | None,
            CatalogActor,
            CatalogActivatedAt,
        ],
        FoundLineageResult,
    ]
    admit_catalog_member: Callable[
        [
            RevisionKind,
            CatalogLineageId,
            PublishedRevisionHash,
            CatalogActor,
            CatalogActivatedAt,
        ],
        AdmitMemberResult,
    ]
    get_occupancy_revision: Callable[[str, str], GetOccupancyResult]
    publish_occupancy_revision: Callable[
        [str, str, int, tuple[tuple[str, str], ...]],
        PublishOccupancyUseCaseResult,
    ]
    get_project_root_revision: Callable[[str], GetProjectRootResult]
    publish_project_root_revision: Callable[
        [str, int, str], PublishProjectRootUseCaseResult
    ]
    admit_queue_item: Callable[[AdmitQueueItem], AdmitQueueItemOutcome]
    list_admitted_queue_items: Callable[
        [QueueItemId | None, int], ListAdmittedQueueItemsOutcome
    ]
    import_project_source_issues: Callable[[], ImportProjectSourceIssuesOutcome]
    list_observed_queue_items: Callable[
        [QueueItemId | None, int], ListObservedQueueItemsOutcome
    ]


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
