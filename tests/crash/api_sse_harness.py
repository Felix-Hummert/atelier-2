from __future__ import annotations

import os
from pathlib import Path

from atelier2.adapters.dbos.runtime import DbosRuntimeSettings, create_canonical_engine
from atelier2.api.app import create_app
from atelier2.ports.agent_executions import AgentExecutorRegistry
from tests.scenarios.api import api_limits, durable_ports, event_poll_backoff

database_path = Path(os.environ["ATELIER2_TEST_DATABASE"])
settings = DbosRuntimeSettings(database_path, os.environ["ATELIER2_TEST_APP_VERSION"])
engine = create_canonical_engine(database_path)

app = create_app(
    source_commit=os.environ["ATELIER2_TEST_SOURCE_COMMIT"],
    source_tree=os.environ["ATELIER2_TEST_SOURCE_TREE"],
    ports=durable_ports(engine, settings, AgentExecutorRegistry()),
    limits=api_limits(event_page_size=2),
    event_poll_backoff=event_poll_backoff(),
)
