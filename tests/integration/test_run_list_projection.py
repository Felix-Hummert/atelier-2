"""A listed run is the published document, and a corrupt list leaves a log."""

from __future__ import annotations

import hashlib
import io
import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.dbos.schema import (
    run_agent_bindings,
    run_configuration_revisions,
    runs,
    workflow_revisions,
)
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
from atelier2.contracts.run_projections import RunPage, RunProjection
from atelier2.contracts.runs import (
    FIRST_ROUND_ORDINAL,
    RunId,
    RunState,
    WorkflowRevision,
)
from atelier2.host.logging import PROCESS_LOGGER_NAME, configure_process_logging
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AgentConfigurationRevisionExisting,
    AuthProfileRevisionCreated,
    AuthProfileRevisionExisting,
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
from tests.scenarios.durable_state import (
    canonical_loopback_effects,
    canonical_runtime_settings,
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
        canonical_runtime_settings(
            tmp_path, "list-projection-test", agent_scratch_root(tmp_path)
        ),
        canonical_loopback_effects(tmp_path),
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
    """The one builder binding every seeded historic run shares.

    Publishing is content-addressed, so a second seeded run republishing the
    same authored bytes is an idempotent republish (`*Existing`), not a
    conflict -- exactly the standing a caller who already holds this binding
    meets on every call after the first.
    """
    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    auth = AuthProfileRevision("max", 1, ProviderId("exact"), AuthMode.SUBSCRIPTION)
    assert isinstance(
        catalog.publish_auth_profile_revision(auth),
        (AuthProfileRevisionCreated, AuthProfileRevisionExisting),
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
        (AgentConfigurationRevisionCreated, AgentConfigurationRevisionExisting),
    )
    return AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )


def _seed_historic_run(runtime: DbosRuntime, run_id: RunId) -> WorkflowRevision:
    """One durable run on the shared historic revision, safe to call per run.

    Several seeded runs share one workflow revision the same way several real
    runs do: the revision row is inserted once and reused, while everything
    keyed by the run itself -- its own row, its bootstrap workflow id, its
    agent binding row -- is written fresh for every call.
    """
    with pytest.raises(WorkflowFormatNotExecutable):
        parse_executable_workflow_document(HISTORIC_V3_DOCUMENT)
    parse_workflow_document(HISTORIC_V3_DOCUMENT)
    revision = WorkflowRevision(HISTORIC_V3_DOCUMENT)
    bindings = _bind_builder(runtime)
    configuration_hash = _digest(f"configuration-{run_id.value}")
    with runtime.engine.begin() as connection:
        already_seeded = connection.execute(
            sa.select(sa.literal(True)).where(
                workflow_revisions.c.revision_hash == revision.revision_hash.value
            )
        ).scalar_one_or_none()
        if already_seeded is None:
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
                "bootstrap_workflow_id": f"historic-workflow-{run_id.value}",
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
    assert all(isinstance(row, RunProjection) for row in page.runs)
    assert [row.run.run_id for row in page.runs if isinstance(row, RunProjection)] == [
        run_id
    ]


def test_a_corrupt_run_becomes_a_defective_row_beside_its_healthy_neighbours(
    runtime: DbosRuntime, process_log: io.StringIO
) -> None:
    """A run list answers for every entry it can (#1042).

    Two healthy runs sit either side of one whose own projection cannot be
    told; the page still answers 200, the healthy rows read as they always
    have, and the corrupt one is told apart as a defective row instead of
    dragging the whole page down to a 500. A single-run read of that same run
    stays fail-loud: there it is one run, and 500 is the honest answer.
    """
    healthy_first = RunId("run-a-healthy")
    healthy_second = RunId("run-c-healthy")
    poison_id = RunId("run-b-poison")
    _seed_historic_run(runtime, healthy_first)
    _seed_historic_run(runtime, poison_id)
    _seed_historic_run(runtime, healthy_second)
    with runtime.engine.begin() as connection:
        connection.execute(
            sa.update(runs)
            .where(runs.c.run_id == poison_id.value)
            .values(current_node_id="missing-node")
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
        API_PREFIX + "/runs/" + encode_public_run_reference(poison_id)
    )

    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert [item["kind"] for item in items] == ["run", "defective", "run"]
    assert [item["run"]["run_id"] for item in items if item["kind"] == "run"] == [
        healthy_first.value,
        healthy_second.value,
    ]
    defective = next(item for item in items if item["kind"] == "defective")
    assert defective["public_run_reference"] == encode_public_run_reference(poison_id)
    assert defective["problem_code"] == "durable-state-corrupt"
    # The curated, bounded reason names the failure's class, never the raw
    # exception text (#1042 review) -- that stays in the journal entry below.
    assert defective["detail"] == "RunTransitionConflict"
    assert inspected.status_code == 500
    assert inspected.json()["type"].endswith("durable-state-corrupt")
    logged = _json_records(process_log.getvalue())
    row_log = next(
        record
        for record in logged
        if record.get("event") == "run_list_projection_corrupt"
        and record.get("run_id") == poison_id.value
    )
    inspected_log = next(
        record
        for record in logged
        if record.get("event") == "run_get_projection_corrupt"
    )
    assert row_log["level"] == "error"
    assert "absent" in str(row_log["exception"]).lower()
    assert inspected_log["level"] == "error"
    assert inspected_log["run_id"] == poison_id.value
    assert inspected_log["public_run_reference"] == encode_public_run_reference(
        poison_id
    )
    assert poison_id.value in str(inspected_log["message"])
    assert "absent" in str(inspected_log["exception"]).lower()
