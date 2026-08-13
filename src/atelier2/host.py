from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.queries import DbosQueries
from atelier2.adapters.dbos.reconciler import DbosEffectReconcileCommander
from atelier2.adapters.dbos.run_store import DbosWaitAnswerer
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.api.app import ApiPorts, create_app
from atelier2.api.limits import ApiLimits
from atelier2.api.stream import EventPollBackoff
from atelier2.contracts.effects import AdapterRevision, EffectDestination

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8422

MAXIMUM_REQUEST_BODY_BYTES = 65_536
MAXIMUM_FIELD_CHARACTERS = 1_024
MAXIMUM_BASE64_CHARACTERS = 65_536
MAXIMUM_DECODED_PAYLOAD_BYTES = 49_152
MAXIMUM_WORKFLOW_NODES = 100
EVENT_PAGE_SIZE = 50
MAXIMUM_CONTROL_QUERIES = 8
MAXIMUM_EVENT_POLL_QUERIES = 2
MAXIMUM_QUERY_ADMISSION_WAIT_MILLISECONDS = 1_000

INITIAL_EVENT_POLL_DELAY_SECONDS = 0.05
MAXIMUM_EVENT_POLL_DELAY_SECONDS = 1.0
EVENT_POLL_DELAY_MULTIPLIER = 2.0


def api_limits() -> ApiLimits:
    return ApiLimits(
        maximum_request_body_bytes=MAXIMUM_REQUEST_BODY_BYTES,
        maximum_field_characters=MAXIMUM_FIELD_CHARACTERS,
        maximum_base64_characters=MAXIMUM_BASE64_CHARACTERS,
        maximum_decoded_payload_bytes=MAXIMUM_DECODED_PAYLOAD_BYTES,
        maximum_workflow_nodes=MAXIMUM_WORKFLOW_NODES,
        event_page_size=EVENT_PAGE_SIZE,
        maximum_control_queries=MAXIMUM_CONTROL_QUERIES,
        maximum_event_poll_queries=MAXIMUM_EVENT_POLL_QUERIES,
        maximum_query_admission_wait_milliseconds=(
            MAXIMUM_QUERY_ADMISSION_WAIT_MILLISECONDS
        ),
    )


def event_poll_backoff() -> EventPollBackoff:
    return EventPollBackoff(
        initial_delay_seconds=INITIAL_EVENT_POLL_DELAY_SECONDS,
        maximum_delay_seconds=MAXIMUM_EVENT_POLL_DELAY_SECONDS,
        multiplier=EVENT_POLL_DELAY_MULTIPLIER,
    )


@dataclass(frozen=True)
class HostSettings:
    database_path: Path
    effect_store_path: Path
    effect_adapter_revision: str
    effect_destination: str
    application_version: str
    source_commit: str
    source_tree: str
    frontend_dist: Path
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    limits: ApiLimits = field(default_factory=api_limits)
    event_poll_backoff: EventPollBackoff = field(default_factory=event_poll_backoff)

    def __post_init__(self) -> None:
        database_path = self.database_path.resolve()
        effect_store_path = self.effect_store_path.resolve()
        frontend_dist = self.frontend_dist.resolve()
        object.__setattr__(self, "database_path", database_path)
        object.__setattr__(self, "effect_store_path", effect_store_path)
        object.__setattr__(self, "frontend_dist", frontend_dist)
        if database_path == effect_store_path:
            raise ValueError("durable database and effect store must be distinct")
        for name in (
            "effect_adapter_revision",
            "effect_destination",
            "application_version",
            "source_commit",
            "source_tree",
            "host",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must be nonempty")
        if type(self.port) is not int or not 1 <= self.port <= 65_535:
            raise ValueError("port must be an integer between 1 and 65535")
        if (
            not (frontend_dist / "index.html").is_file()
            or not (frontend_dist / "assets").is_dir()
        ):
            raise ValueError("frontend distribution must contain index.html and assets")


def compose_application(settings: HostSettings) -> tuple[FastAPI, DbosRuntime]:
    runtime = DbosRuntime(
        DbosRuntimeSettings(settings.database_path, settings.application_version),
        LoopbackEffectAdapterFactory(
            settings.effect_store_path,
            AdapterRevision(settings.effect_adapter_revision),
            EffectDestination(settings.effect_destination),
        ),
        ExactOutputAgentExecutorFactory(),
    )
    try:
        queries = DbosQueries(runtime.engine)
        app = create_app(
            source_commit=settings.source_commit,
            source_tree=settings.source_tree,
            ports=ApiPorts(
                workflow_revision_publisher=DbosWorkflowRevisionPublisher(
                    runtime.engine
                ),
                published_run_starter=DbosDurableRunStarter(
                    runtime.engine,
                    runtime.settings,
                    runtime.agent_executor_registry,
                ),
                wait_answerer=DbosWaitAnswerer(
                    runtime.engine, runtime.settings.application_version
                ),
                reconcile_commander=DbosEffectReconcileCommander(
                    runtime.engine, runtime.settings
                ),
                workflow_revision_queries=queries,
                run_queries=queries,
                run_event_queries=queries,
                workflow_document_parser=parse_workflow_document,
                agent_configuration_catalog=DbosAgentConfigurationCatalog(
                    runtime.engine, runtime.agent_executor_registry
                ),
                agent_attempt_canceller=DbosAgentAttemptStore(
                    runtime.engine, runtime.settings.application_version
                ),
            ),
            limits=settings.limits,
            event_poll_backoff=settings.event_poll_backoff,
            frontend_dist=settings.frontend_dist,
        )
        runtime.launch()
        return app, runtime
    except BaseException:
        runtime.close()
        raise


def serve(settings: HostSettings) -> None:
    app, runtime = compose_application(settings)
    try:
        uvicorn.Server(
            uvicorn.Config(app, host=settings.host, port=settings.port)
        ).run()
    finally:
        runtime.close()


def main(arguments: Sequence[str] | None = None) -> None:
    parser = _argument_parser()
    parsed = parser.parse_args(arguments)
    if parsed.command != "serve":
        parser.error("a command is required")
    try:
        serve(
            HostSettings(
                database_path=parsed.database,
                effect_store_path=parsed.effect_store,
                effect_adapter_revision=parsed.effect_adapter_revision,
                effect_destination=parsed.effect_destination,
                application_version=parsed.application_version,
                source_commit=parsed.source_commit,
                source_tree=parsed.source_tree,
                frontend_dist=parsed.frontend_dist,
                host=parsed.host,
                port=parsed.port,
            )
        )
    except KeyboardInterrupt:
        return


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="atelier2")
    commands = parser.add_subparsers(dest="command")
    serve_parser = commands.add_parser("serve", help="serve the local cockpit")
    serve_parser.add_argument("--database", type=Path, required=True)
    serve_parser.add_argument("--effect-store", type=Path, required=True)
    serve_parser.add_argument("--effect-adapter-revision", required=True)
    serve_parser.add_argument("--effect-destination", required=True)
    serve_parser.add_argument("--application-version", required=True)
    serve_parser.add_argument("--source-commit", required=True)
    serve_parser.add_argument("--source-tree", required=True)
    serve_parser.add_argument("--frontend-dist", type=Path, required=True)
    serve_parser.add_argument("--host", default=DEFAULT_HOST)
    serve_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    return parser
