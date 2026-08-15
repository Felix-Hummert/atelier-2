from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from atelier2.api.context import ApiContext, ApiPorts, install_api_context
from atelier2.api.limits import ApiLimits, RequestBodyLimitMiddleware
from atelier2.api.openapi import API_PREFIX, install_custom_openapi
from atelier2.api.problems import install_problem_handlers
from atelier2.api.routes import agents, events, health, revisions, runs
from atelier2.api.stream import BoundedQueryRunner, EventPollBackoff
from atelier2.application.publish_workflow_revision import WorkflowPublicationLimits


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
    install_api_context(
        app,
        ApiContext(
            source_commit=source_commit,
            source_tree=source_tree,
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
            workflow_projection_limit=WorkflowPublicationLimits(
                maximum_document_bytes=min(
                    limits.maximum_request_body_bytes,
                    limits.maximum_base64_decoded_bytes,
                ),
                maximum_nodes=limits.maximum_workflow_nodes,
                maximum_string_characters=limits.maximum_field_characters,
                maximum_payload_bytes=min(
                    limits.maximum_decoded_payload_bytes,
                    limits.maximum_base64_decoded_bytes,
                ),
            ),
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


def _mount_frontend(app: FastAPI, frontend_dist: Path) -> None:
    index_file, assets_directory = _frontend_distribution(frontend_dist)
    app.mount(
        "/atelier/assets",
        StaticFiles(directory=assets_directory, check_dir=True),
        name="atelier-assets",
    )

    async def frontend_index() -> FileResponse:
        return FileResponse(index_file, media_type="text/html")

    for path in (
        "/atelier",
        "/atelier/",
        "/atelier/runs",
        "/atelier/new",
        "/atelier/runs/{public_ref}",
    ):
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
