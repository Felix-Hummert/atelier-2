"""A listed run is the published document, and a corrupt list leaves a log."""

from __future__ import annotations

import hashlib
import io
import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import (
    run_agent_bindings,
    run_configuration_revisions,
    runs,
    workflow_revisions,
)
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.adapters.yaml_workflows import (
    WorkflowFormatNotExecutable,
    parse_executable_workflow_document,
    parse_workflow_document,
)
from atelier2.api.app import create_app
from atelier2.api.openapi import API_PREFIX
from atelier2.api.references import encode_public_run_reference
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutorRevision,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.run_projections import RunPage
from atelier2.contracts.runs import (
    FIRST_ROUND_ORDINAL,
    RunId,
    RunState,
    WorkflowRevision,
)
from atelier2.host.logging import PROCESS_LOGGER_NAME, configure_process_logging
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AuthProfileRevisionCreated,
)
from atelier2.ports.run_queries import RunFound
from tests.scenarios.agents import (
    agent_scratch_root,
    failing_agent_executor_factory,
)
from tests.scenarios.api import (
    api_limits,
    api_ports,
    durable_queries,
    event_poll_backoff,
)

HISTORIC_V3_DOCUMENT = b"""format_version: 3
name: Historic chain
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Do the one thing this chain is for.
"""


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    started = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "list-projection-test",
            agent_scratch_root=agent_scratch_root(tmp_path),
        ),
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        ExactOutputAgentExecutorFactory(),
        (failing_agent_executor_factory("exact", []),),
    )
    started.initialize_storage()
    try:
        yield started
    finally:
        started.close()


@pytest.fixture
def process_log() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    watched = ("", PROCESS_LOGGER_NAME, "uvicorn", "uvicorn.error", "uvicorn.access")
    snapshot = tuple(_logger_snapshot(name) for name in watched)
    configure_process_logging(stream)
    try:
        yield stream
    finally:
        for name, level, handlers, propagate, disabled in snapshot:
            logger = logging.getLogger(name)
            logger.handlers[:] = handlers
            logger.setLevel(level)
            logger.propagate = propagate
            logger.disabled = disabled


def _logger_snapshot(
    name: str,
) -> tuple[str, int, list[logging.Handler], bool, bool]:
    logger = logging.getLogger(name)
    return (
        name,
        logger.level,
        list(logger.handlers),
        logger.propagate,
        logger.disabled,
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _bind_builder(runtime: DbosRuntime) -> AgentBindingSet:
    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    auth = AuthProfileRevision("max", 1, ProviderId("exact"), AuthMode.SUBSCRIPTION)
    assert isinstance(
        catalog.publish_auth_profile_revision(auth), AuthProfileRevisionCreated
    )
    configuration = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision("exact/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    assert isinstance(
        catalog.publish_agent_configuration_revision(configuration),
        AgentConfigurationRevisionCreated,
    )
    return AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )


def _seed_historic_run(runtime: DbosRuntime, run_id: RunId) -> WorkflowRevision:
    with pytest.raises(WorkflowFormatNotExecutable):
        parse_executable_workflow_document(HISTORIC_V3_DOCUMENT)
    parse_workflow_document(HISTORIC_V3_DOCUMENT)
    revision = WorkflowRevision(HISTORIC_V3_DOCUMENT)
    bindings = _bind_builder(runtime)
    configuration_hash = _digest(f"configuration-{run_id.value}")
    with runtime.engine.begin() as connection:
        connection.execute(
            workflow_revisions.insert(),
            {
                "revision_hash": revision.revision_hash.value,
                "document": revision.document,
            },
        )
        connection.execute(
            run_configuration_revisions.insert(),
            {
                "revision_hash": configuration_hash,
                "preimage": b"seeded historic run configuration",
            },
        )
        connection.execute(
            runs.insert(),
            {
                "run_id": run_id.value,
                "bootstrap_workflow_id": "historic-workflow",
                "revision_hash": revision.revision_hash.value,
                "workflow_format_version": 3,
                "agent_binding_set_hash": bindings.binding_set_hash.value,
                "run_configuration_revision_hash": configuration_hash,
                "current_node_id": "implement",
                "current_round_ordinal": FIRST_ROUND_ORDINAL,
                "state": RunState.STARTED.value,
                "state_version": 0,
                "last_event_sequence": 0,
                "terminal_hash": None,
            },
        )
        connection.execute(
            run_agent_bindings.insert(),
            {
                "run_id": run_id.value,
                "revision_hash": revision.revision_hash.value,
                "binding_set_hash": bindings.binding_set_hash.value,
                "role": "builder",
                "agent_configuration_revision_hash": (
                    bindings.bindings[0].agent_configuration_revision_hash.value
                ),
            },
        )
    return revision


def _json_records(text: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in text.splitlines() if line.startswith("{")]


def test_a_historic_v3_run_lists_even_when_today_would_refuse_to_start_it(
    runtime: DbosRuntime,
) -> None:
    run_id = RunId("historic/unstartable-today")
    _seed_historic_run(runtime, run_id)
    queries = durable_queries(runtime.engine)

    found = queries.get_run(run_id)
    page = queries.list_runs(None, 5)

    assert isinstance(found, RunFound)
    assert found.projection.run.run_id == run_id
    assert isinstance(page, RunPage)
    assert [item.run.run_id for item in page.runs] == [run_id]


def test_a_corrupt_run_list_logs_the_reason_it_refused(
    runtime: DbosRuntime, process_log: io.StringIO
) -> None:
    revision = WorkflowRevision(
        b"""format_version: 1
start: agent
nodes:
  - {id: agent, type: agent, job: test, output: result, next: final}
  - {id: final, type: subworkflow, operation: add, operands: [2, 3], next: null}
"""
    )
    with runtime.engine.begin() as connection:
        connection.execute(
            workflow_revisions.insert(),
            {
                "revision_hash": revision.revision_hash.value,
                "document": revision.document,
            },
        )
        connection.execute(
            runs.insert(),
            {
                "run_id": "poison-row",
                "bootstrap_workflow_id": "poison-workflow",
                "revision_hash": revision.revision_hash.value,
                "workflow_format_version": 1,
                "agent_binding_set_hash": None,
                "current_node_id": "missing-node",
                "current_round_ordinal": FIRST_ROUND_ORDINAL,
                "state": RunState.STARTED.value,
                "state_version": 0,
                "last_event_sequence": 0,
                "terminal_hash": None,
            },
        )
    client = TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=api_ports(run_queries=durable_queries(runtime.engine)),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        ),
        raise_server_exceptions=False,
    )

    listed = client.get(API_PREFIX + "/runs?limit=5")
    inspected = client.get(
        API_PREFIX + "/runs/" + encode_public_run_reference(RunId("poison-row"))
    )

    assert listed.status_code == 500
    assert listed.json()["type"].endswith("durable-state-corrupt")
    assert inspected.status_code == 500
    assert inspected.json()["type"].endswith("durable-state-corrupt")
    events = {
        record["event"]: record for record in _json_records(process_log.getvalue())
    }
    listed_log = events["run_list_projection_corrupt"]
    inspected_log = events["run_get_projection_corrupt"]
    poison_id = RunId("poison-row")
    assert listed_log["level"] == "error"
    assert inspected_log["level"] == "error"
    assert inspected_log["run_id"] == poison_id.value
    assert inspected_log["public_run_reference"] == encode_public_run_reference(
        poison_id
    )
    assert poison_id.value in str(inspected_log["message"])
    assert "absent" in str(listed_log["exception"]).lower()
    assert "absent" in str(inspected_log["exception"]).lower()
