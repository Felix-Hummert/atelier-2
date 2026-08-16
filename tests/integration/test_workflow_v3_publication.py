from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from httpx import Response

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.queries import DbosQueries
from atelier2.adapters.dbos.reconciler import DbosEffectReconcileCommander
from atelier2.adapters.dbos.run_store import DbosWaitAnswerer
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import runs, workflow_revisions
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.api.app import create_app
from atelier2.api.context import ApiPorts
from atelier2.api.openapi import API_PREFIX
from atelier2.api.references import encode_canonical_base64
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from tests.scenarios.api import api_limits, event_poll_backoff
from tests.scenarios.runtime import exact_output_runtime
from tests.scenarios.workflows import (
    V3_CONTROL_EDGE_LINE,
    V3_DOCUMENT,
    V3_DOCUMENT_NAME,
    V3_NODE_COUNT,
)

V1_DOCUMENT = b"""format_version: 1
start: agent
nodes:
  - {id: final, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: agent, type: agent, job: test, output: payload, next: final}
"""


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    configured = exact_output_runtime(
        DbosRuntimeSettings(tmp_path / "atelier.sqlite", "v3-publication-tests"),
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
    )
    configured.initialize_storage()
    try:
        yield configured
    finally:
        configured.close()


def _client(runtime: DbosRuntime) -> TestClient:
    queries = DbosQueries(runtime.engine)
    return TestClient(
        create_app(
            source_commit="commit-under-test",
            source_tree="tree-under-test",
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
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        )
    )


def _publish(client: TestClient, document: bytes) -> Response:
    return client.post(
        API_PREFIX + "/workflow-revisions",
        content=document,
        headers={"content-type": "application/yaml"},
    )


def _row_count(runtime: DbosRuntime, table: sa.Table) -> int:
    with runtime.engine.connect() as connection:
        return int(
            connection.scalar(sa.select(sa.func.count()).select_from(table)) or 0
        )


def test_a_valid_v3_document_publishes_as_one_immutable_hash_identified_revision(
    runtime: DbosRuntime,
) -> None:
    client = _client(runtime)

    created = _publish(client, V3_DOCUMENT)
    retried = _publish(client, V3_DOCUMENT)

    assert (created.status_code, retried.status_code) == (201, 200)
    assert created.json() == retried.json()
    assert created.json()["revision_hash"] == hashlib.sha256(V3_DOCUMENT).hexdigest()
    assert created.json()["document_base64"] == encode_canonical_base64(V3_DOCUMENT)
    assert _row_count(runtime, workflow_revisions) == 1


def test_the_published_v3_revision_reads_back_naming_its_format_as_unexecutable(
    runtime: DbosRuntime,
) -> None:
    client = _client(runtime)
    published = _publish(client, V3_DOCUMENT)
    revision_hash = published.json()["revision_hash"]

    read = client.get(API_PREFIX + f"/workflow-revisions/{revision_hash}")

    assert read.status_code == 200
    assert read.json() == published.json()
    assert read.json()["graph"] == {
        "format_version": 3,
        "executable": False,
        "node_count": V3_NODE_COUNT,
        "name": V3_DOCUMENT_NAME,
        "description": None,
    }
    assert client.get(API_PREFIX + "/workflow-revisions").json() == {
        "items": [{"revision_hash": revision_hash}],
        "next_after_revision_hash": None,
    }


def test_starting_a_run_on_a_v3_revision_is_refused_by_name_and_writes_no_run(
    runtime: DbosRuntime,
) -> None:
    client = _client(runtime)
    revision_hash = _publish(client, V3_DOCUMENT).json()["revision_hash"]

    refused = client.post(
        API_PREFIX + "/runs",
        json={"run_id": "v3-run", "workflow_revision_hash": revision_hash},
    )

    assert refused.status_code == 409
    assert refused.json()["type"].endswith(":workflow-format-not-executable")
    assert _row_count(runtime, runs) == 0


def test_a_v1_revision_published_beside_a_v3_one_still_starts_its_run(
    runtime: DbosRuntime,
) -> None:
    client = _client(runtime)
    _publish(client, V3_DOCUMENT)
    executable_hash = _publish(client, V1_DOCUMENT).json()["revision_hash"]

    started = client.post(
        API_PREFIX + "/runs",
        json={"run_id": "v1-run", "workflow_revision_hash": executable_hash},
    )

    assert started.status_code == 201
    assert started.json()["workflow_revision_hash"] == executable_hash
    assert _row_count(runtime, runs) == 1


@pytest.mark.parametrize(
    ("broken", "expected_fragments"),
    [
        pytest.param(
            V3_DOCUMENT.replace(V3_CONTROL_EDGE_LINE, b""),
            ("'review'", "'inputs'", "data_edge_outside_closure"),
            id="data edge outside the depends_on closure",
        ),
        pytest.param(
            V3_DOCUMENT.replace(
                b"    role: reviewer\n", b"    role: reviewer\n    next: done\n"
            ),
            ("'review'", "'next'", "retired_key", "depends_on"),
            id="retired V1 key",
        ),
        pytest.param(
            V3_DOCUMENT.replace(
                b"    instruction: Name every",
                b"    speed: fast\n    instruction: Name every",
            ),
            ("'review'", "'speed'", "unknown_field"),
            id="unknown field",
        ),
        pytest.param(
            V3_DOCUMENT.replace(
                b"    type: agent\n    role: reviewer",
                b"    type: mystery\n    role: reviewer",
            ),
            ("'review'", "'type'", "invalid_value"),
            id="unknown node kind",
        ),
        pytest.param(
            V3_DOCUMENT.replace(
                b"    type: agent\n    role: reviewer", b"    role: reviewer"
            ),
            ("'review'", "'type'", "missing_field"),
            id="missing node kind",
        ),
        pytest.param(
            V3_DOCUMENT + b"format_version: 3\n",
            ("'format_version'", "duplicate_key"),
            id="unsafe YAML duplicate key",
        ),
        pytest.param(
            V3_DOCUMENT.replace(b"format_version: 3", b"format_version: !!int 3", 1),
            ("'tag'", "forbidden_yaml_feature"),
            id="unsafe YAML explicit tag",
        ),
        pytest.param(
            V3_DOCUMENT.replace(
                b"    outputs:\n      - name: candidate\n",
                b"    outputs: "
                + b"[" * 40
                + b"]" * 40
                + b"\n      - name: candidate\n",
            ),
            ("'nesting'", "document_too_deep"),
            id="unsafe YAML nested past the bound",
        ),
    ],
)
def test_an_invalid_v3_document_is_refused_naming_its_node_and_field(
    runtime: DbosRuntime, broken: bytes, expected_fragments: tuple[str, ...]
) -> None:
    client = _client(runtime)

    refused = _publish(client, broken)

    assert refused.status_code == 422
    assert refused.json()["type"].endswith(":invalid-workflow-document")
    detail = refused.json()["detail"]
    assert all(fragment in detail for fragment in expected_fragments), detail
    assert _row_count(runtime, workflow_revisions) == 0
