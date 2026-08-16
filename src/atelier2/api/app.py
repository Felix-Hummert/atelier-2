from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from atelier2.api.context import (
    ApiContext,
    ApiPorts,
    ApiUseCases,
    install_api_context,
)
from atelier2.api.limits import (
    ApiLimits,
    RequestBodyLimitMiddleware,
    durable_projection_limit,
)
from atelier2.api.openapi import API_PREFIX, install_custom_openapi
from atelier2.api.problems import install_problem_handlers
from atelier2.api.routes import agents, events, health, revisions, runs
from atelier2.api.stream import BoundedQueryRunner, EventPollBackoff
from atelier2.application.answer_wait import answer_wait_result
from atelier2.application.prepare_run_events import prepare_run_events
from atelier2.application.publish_agent_configurations import (
    publish_agent_configuration_revision,
    publish_auth_profile_revision,
)
from atelier2.application.publish_workflow_revision import (
    WorkflowPublicationLimits,
    publish_workflow_revision,
)
from atelier2.application.read_run_events import read_run_events
from atelier2.application.read_runs import get_run, list_runs
from atelier2.application.read_workflow_revisions import (
    get_workflow_revision,
    list_described_workflow_revisions,
    list_workflow_revisions,
)
from atelier2.application.reconcile_run import reconcile_run
from atelier2.application.start_published_run import start_published_run
from atelier2.ports.workflow_revisions import (
    EnrichedPageBudget,
)


def bound_use_cases(
    ports: ApiPorts,
    projection_limit: WorkflowPublicationLimits,
    enriched_page_budget: EnrichedPageBudget,
) -> ApiUseCases:
    """Spend the ports here, so that nothing below this line can reach one."""
    return ApiUseCases(
        get_workflow_revision=lambda revision_hash: get_workflow_revision(
            revision_hash, ports.workflow_revision_queries
        ),
        list_workflow_revisions=lambda after, limit: list_workflow_revisions(
            after, limit, ports.workflow_revision_queries
        ),
        list_described_workflow_revisions=(
            lambda after, limit: list_described_workflow_revisions(
                after,
                limit,
                enriched_page_budget,
                ports.workflow_revision_queries,
            )
        ),
        get_run=lambda run_id: get_run(run_id, ports.run_queries),
        list_runs=lambda after, limit: list_runs(after, limit, ports.run_queries),
        prepare_run_events=lambda run_id, after_sequence: prepare_run_events(
            run_id, after_sequence, ports.run_event_queries
        ),
        read_run_events=lambda run_id, after_sequence, page_size: read_run_events(
            run_id,
            after_sequence,
            page_size,
            ports.run_event_queries,
        ),
        publish_workflow_revision=lambda document: publish_workflow_revision(
            document,
            ports.workflow_revision_publisher,
            ports.workflow_document_parser,
            projection_limit,
        ),
        publish_auth_profile_revision=(
            lambda profile_id, revision_number, provider_id, auth_mode: (
                publish_auth_profile_revision(
                    profile_id,
                    revision_number,
                    provider_id,
                    auth_mode,
                    ports.agent_configuration_catalog,
                )
            )
        ),
        publish_agent_configuration_revision=(
            lambda model, auth_profile_hash, executor_revision, capability: (
                publish_agent_configuration_revision(
                    model,
                    auth_profile_hash,
                    executor_revision,
                    capability,
                    ports.agent_configuration_catalog,
                )
            )
        ),
        start_published_run=lambda run_id, revision_hash, bindings: start_published_run(
            run_id, revision_hash, bindings, ports.published_run_starter
        ),
        answer_wait=lambda run_id, revision_hash, node_id, answer_bytes: (
            answer_wait_result(
                run_id, revision_hash, node_id, answer_bytes, ports.wait_answerer
            )
        ),
        reconcile_run=lambda request: reconcile_run(
            request, ports.run_queries, ports.reconcile_commander
        ),
    )


def create_app(
    *,
    source_commit: str,
    source_tree: str,
    ports: ApiPorts,
    limits: ApiLimits,
    event_poll_backoff: EventPollBackoff,
    frontend_dist: Path | None = None,
) -> FastAPI:
    if not source_commit:
        raise ValueError("source_commit must be injected at application construction")
    if not source_tree:
        raise ValueError("source_tree must be injected at application construction")
    app = FastAPI(
        title="Atelier 2 durable workflow API",
        version="1",
        openapi_url=API_PREFIX + "/openapi.json",
        docs_url=None,
        redoc_url=None,
    )
    install_problem_handlers(app, versioned_run_start_path=API_PREFIX + "/runs")
    app.add_middleware(
        RequestBodyLimitMiddleware,
        maximum_body_bytes=limits.maximum_request_body_bytes,
        api_prefix=API_PREFIX,
    )
    admission_timeout_seconds = limits.maximum_query_admission_wait_milliseconds / 1_000
    workflow_projection_limit = durable_projection_limit(limits)
    install_api_context(
        app,
        ApiContext(
            source_commit=source_commit,
            source_tree=source_tree,
            use_cases=bound_use_cases(
                ports,
                workflow_projection_limit,
                EnrichedPageBudget(
                    maximum_nodes=limits.maximum_enriched_page_nodes,
                    maximum_document_bytes=(
                        limits.maximum_enriched_page_document_bytes
                    ),
                ),
            ),
            ports=ports,
            limits=limits,
            control_runner=BoundedQueryRunner(
                limits.maximum_control_queries,
                admission_timeout_seconds=admission_timeout_seconds,
            ),
            event_runner=BoundedQueryRunner(
                limits.maximum_event_poll_queries,
                admission_timeout_seconds=admission_timeout_seconds,
            ),
            workflow_projection_limit=workflow_projection_limit,
            event_poll_backoff=event_poll_backoff,
        ),
    )

    if frontend_dist is not None:
        _mount_frontend(app, frontend_dist)

    # The order of these five calls is the order of the published document's
    # `paths` keys, which the frozen artefact pins byte for byte.
    app.include_router(health.router)
    app.include_router(agents.router)
    app.include_router(revisions.router)
    app.include_router(runs.router)
    app.include_router(events.router)

    install_custom_openapi(app)
    return app


# Every path the browser can be given cold — reloaded, pasted, bookmarked — must
# hand back the application instead of a route refusal, and which paths those are
# is the client router's decision, not this server's. The browser declares them in
# `frontend/src/lib/servedPaths.json`; `tests/host/test_local_host.py` reads that
# declaration and fails when this tuple does not serve exactly it, because the two
# runtimes cannot import one list.
COCKPIT_INDEX_PATHS: tuple[str, ...] = (
    "/atelier",
    "/atelier/",
    "/atelier/project",
    "/atelier/runs",
    "/atelier/new",
    "/atelier/runs/{public_ref}",
)


def _mount_frontend(app: FastAPI, frontend_dist: Path) -> None:
    index_file, assets_directory = _frontend_distribution(frontend_dist)
    app.mount(
        "/atelier/assets",
        StaticFiles(directory=assets_directory, check_dir=True),
        name="atelier-assets",
    )

    async def frontend_index() -> FileResponse:
        return FileResponse(index_file, media_type="text/html")

    for path in COCKPIT_INDEX_PATHS:
        app.add_api_route(
            path,
            frontend_index,
            methods=["GET"],
            include_in_schema=False,
        )


def _frontend_distribution(frontend_dist: Path) -> tuple[Path, Path]:
    distribution = frontend_dist.resolve()
    index_file = distribution / "index.html"
    assets_directory = distribution / "assets"
    if not index_file.is_file() or not assets_directory.is_dir():
        raise ValueError(
            "frontend distribution must contain a readable index.html and assets directory"
        )
    try:
        index_file.open("rb").close()
        next(assets_directory.iterdir(), None)
    except OSError as error:
        raise ValueError("frontend distribution must be readable") from error
    return index_file, assets_directory
