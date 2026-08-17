from __future__ import annotations

import os
from pathlib import Path

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.reconciler import DbosEffectReconcileCommander
from atelier2.adapters.dbos.run_store import DbosWaitAnswerer
from atelier2.adapters.dbos.runtime import DbosRuntimeSettings, create_canonical_engine
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.api.app import create_app
from atelier2.api.context import ApiPorts
from atelier2.ports.agent_executions import AgentExecutorRegistry
from tests.scenarios.api import api_limits, durable_queries, event_poll_backoff

database_path = Path(os.environ["ATELIER2_TEST_DATABASE"])
settings = DbosRuntimeSettings(database_path, os.environ["ATELIER2_TEST_APP_VERSION"])
engine = create_canonical_engine(database_path)
queries = durable_queries(engine)

app = create_app(
    source_commit=os.environ["ATELIER2_TEST_SOURCE_COMMIT"],
    source_tree=os.environ["ATELIER2_TEST_SOURCE_TREE"],
    ports=ApiPorts(
        DbosWorkflowRevisionPublisher(engine),
        DbosDurableRunStarter(engine, settings, AgentExecutorRegistry()),
        DbosWaitAnswerer(engine, settings.application_version),
        DbosEffectReconcileCommander(engine, settings),
        queries,
        queries,
        queries,
        parse_workflow_document,
        DbosAgentConfigurationCatalog(engine, AgentExecutorRegistry()),
        DbosAgentAttemptStore(engine, settings.application_version),
        DbosCatalogStore(engine),
        DbosCatalogStore(engine),
        DbosCatalogStore(engine),
    ),
    limits=api_limits(event_page_size=2),
    event_poll_backoff=event_poll_backoff(),
)
