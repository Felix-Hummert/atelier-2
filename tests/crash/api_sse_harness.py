from __future__ import annotations

import os
from pathlib import Path

from atelier2.adapters.dbos.queries import DbosQueries
from atelier2.adapters.dbos.reconciler import DbosEffectReconcileCommander
from atelier2.adapters.dbos.run_store import DbosWaitAnswerer
from atelier2.adapters.dbos.runtime import DbosRuntimeSettings, create_canonical_engine
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.api.app import ApiPorts, create_app

database_path = Path(os.environ["ATELIER2_TEST_DATABASE"])
settings = DbosRuntimeSettings(database_path, os.environ["ATELIER2_TEST_APP_VERSION"])
engine = create_canonical_engine(database_path)
queries = DbosQueries(engine)

app = create_app(
    source_commit=os.environ["ATELIER2_TEST_SOURCE_COMMIT"],
    source_tree=os.environ["ATELIER2_TEST_SOURCE_TREE"],
    ports=ApiPorts(
        DbosWorkflowRevisionPublisher(engine),
        DbosDurableRunStarter(engine, settings),
        DbosWaitAnswerer(engine, settings.application_version),
        DbosEffectReconcileCommander(engine, settings),
        queries,
        queries,
        queries,
    ),
    event_page_size=2,
    event_poll_delay_seconds=0.01,
)
