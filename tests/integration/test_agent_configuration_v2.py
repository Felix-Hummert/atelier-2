from __future__ import annotations

import time
from pathlib import Path
from typing import cast

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.queries import DbosQueries
from atelier2.adapters.dbos.reconciler import DbosEffectReconcileCommander
from atelier2.adapters.dbos.run_store import DbosWaitAnswerer
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import (
    agent_receipts_v2,
    run_agent_bindings,
    run_events,
    runs,
)
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.dbos.workflow import (
    EncodedAgentBindingV2,
    RunBindingConflict,
    _agent_request_v2,
)
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.api.app import ApiPorts, create_app
from atelier2.api.openapi import API_PREFIX
from atelier2.api.references import encode_public_run_reference
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutionRequestV2,
    AgentExecutionResult,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    AgentOutputLimitExceeded,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.run_bindings import RunV2
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AuthProfileRevisionCreated,
)
from atelier2.ports.durable_runs import DurableRunCreated, StartPublishedRunRequestV2
from atelier2.ports.run_queries import RunFound, RunPage
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_attempt_execution,
)
from tests.scenarios.api import api_limits, event_poll_backoff

_DOCUMENT = b"""format_version: 2
start: build
nodes:
  - {id: done, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: review, type: agent, role: reviewer, job: review, next: done}
  - {id: build, type: agent, role: builder, job: build, next: review}
"""


def _effect_factory(root: Path) -> LoopbackEffectAdapterFactory:
    return LoopbackEffectAdapterFactory(
        root / "effects.sqlite",
        AdapterRevision("loopback-v1"),
        EffectDestination("test"),
    )


def _runtime(
    root: Path, factories: tuple[RecordingAgentExecutorFactoryV2, ...]
) -> DbosRuntime:
    return DbosRuntime(
        DbosRuntimeSettings(root / "atelier.sqlite", "v2-test"),
        _effect_factory(root),
        ExactOutputAgentExecutorFactory(),
        factories,
    )


def _api_client(runtime: DbosRuntime) -> TestClient:
    queries = DbosQueries(runtime.engine)
    return TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=ApiPorts(
                workflow_revision_publisher=DbosWorkflowRevisionPublisher(
                    runtime.engine
                ),
                published_run_starter=DbosDurableRunStarter(
                    runtime.engine, runtime.settings, runtime.agent_executor_registry
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
            ),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        )
    )


def _publish_matrix(
    runtime: DbosRuntime,
) -> tuple[WorkflowRevision, AgentBindingSet]:
    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    bindings = []
    for role, profile, provider, model, revision in (
        ("builder", "max", "anthropic", "opus", "claude-cli/v1"),
        ("reviewer", "chatgpt", "openai", "gpt-5.6", "codex-cli/v1"),
    ):
        auth = AuthProfileRevision(
            profile, 1, ProviderId(provider), AuthMode.SUBSCRIPTION
        )
        assert isinstance(
            catalog.publish_auth_profile_revision(auth), AuthProfileRevisionCreated
        )
        configuration = AgentConfigurationRevision(
            model,
            auth.revision_hash,
            AgentExecutorRevision(revision),
            AgentExecutionCapability.HEADLESS,
            AgentConfigurationRevisionFormatVersion.V2,
        )
        assert isinstance(
            catalog.publish_agent_configuration_revision(configuration),
            AgentConfigurationRevisionCreated,
        )
        bindings.append(AgentBinding(AgentRole(role), configuration.revision_hash))
    workflow = WorkflowRevision(_DOCUMENT)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    return workflow, AgentBindingSet(tuple(bindings))


def _encoded_binding(
    configuration: AgentConfigurationRevision,
    auth: AuthProfileRevision,
    *,
    include_contract: bool,
) -> EncodedAgentBindingV2:
    encoded: dict[str, object] = {
        "type": "agent-v2",
        "role": "builder",
        "job": "build",
        "configuration_hash": configuration.revision_hash.value,
        "auth_hash": auth.revision_hash.value,
        "profile_id": auth.profile_id,
        "revision_number": auth.revision_number,
        "provider_id": auth.provider_id.value,
        "auth_mode": auth.auth_mode.value,
        "model": configuration.model,
        "executor_revision": configuration.executor_revision.value,
    }
    if include_contract:
        encoded.update(
            {
                "revision_format_version": int(configuration.revision_format_version),
                "requested_capability": configuration.requested_capability.value,
            }
        )
    return cast(EncodedAgentBindingV2, encoded)


def test_old_node_binding_payload_replays_and_new_payload_carries_contract() -> None:
    auth = AuthProfileRevision("max", 1, ProviderId("anthropic"), AuthMode.SUBSCRIPTION)
    legacy = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision("claude-cli/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V1,
    )
    current = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision("claude-cli/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    run_id = RunId("capability/replay")
    revision_hash = WorkflowRevision(
        b"format_version: 1\nstart: x\nnodes: []\n"
    ).revision_hash
    operational_identity = AgentExecutorOperationalIdentity("executor/test")

    replayed = _agent_request_v2(
        _encoded_binding(legacy, auth, include_contract=False),
        run_id,
        revision_hash,
        "build",
        operational_identity,
    )
    newly_encoded = _agent_request_v2(
        _encoded_binding(current, auth, include_contract=True),
        run_id,
        revision_hash,
        "build",
        operational_identity,
    )

    assert replayed.resolved_binding.configuration == legacy
    assert newly_encoded.resolved_binding.configuration == current


@pytest.mark.parametrize(
    ("revision_format_version", "requested_capability"),
    (
        (1, "interactive"),
        (1, None),
        (None, "headless"),
        (3, "headless"),
        (True, "headless"),
        (2.0, "headless"),
        (2, 1),
    ),
)
def test_node_binding_payload_refuses_partial_or_invalid_capability_contract(
    revision_format_version: object | None,
    requested_capability: object | None,
) -> None:
    auth = AuthProfileRevision("max", 1, ProviderId("anthropic"), AuthMode.SUBSCRIPTION)
    current = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision("claude-cli/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    encoded = dict(_encoded_binding(current, auth, include_contract=False))
    if revision_format_version is not None:
        encoded["revision_format_version"] = revision_format_version
    if requested_capability is not None:
        encoded["requested_capability"] = requested_capability

    with pytest.raises(RunBindingConflict):
        _agent_request_v2(
            cast(EncodedAgentBindingV2, encoded),
            RunId("capability/invalid-replay"),
            WorkflowRevision(b"format_version: 1\nstart: x\nnodes: []\n").revision_hash,
            "build",
            AgentExecutorOperationalIdentity("executor/test"),
        )


@pytest.mark.parametrize(
    "encoded_contract",
    (
        {"revision_format_version": None, "requested_capability": None},
        {"revision_format_version": None, "requested_capability": "headless"},
        {"revision_format_version": 2, "requested_capability": None},
    ),
)
def test_node_binding_payload_refuses_explicit_null_contract_values(
    encoded_contract: dict[str, object],
) -> None:
    auth = AuthProfileRevision("max", 1, ProviderId("anthropic"), AuthMode.SUBSCRIPTION)
    configuration = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision("claude-cli/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    encoded = dict(_encoded_binding(configuration, auth, include_contract=False))
    encoded.update(encoded_contract)

    with pytest.raises(RunBindingConflict, match="wrong type"):
        _agent_request_v2(
            cast(EncodedAgentBindingV2, encoded),
            RunId("capability/null-replay"),
            WorkflowRevision(b"format_version: 1\nstart: x\nnodes: []\n").revision_hash,
            "build",
            AgentExecutorOperationalIdentity("executor/test"),
        )


def _wait_completed(runtime: DbosRuntime, run_id: RunId) -> RunV2:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = DbosQueries(runtime.engine).get_run(run_id)
        if (
            isinstance(result, RunFound)
            and result.projection.run.state is RunState.COMPLETED
        ):
            assert isinstance(result.projection.run, RunV2)
            return result.projection.run
        time.sleep(0.025)
    raise AssertionError("V2 run did not complete")


def test_two_provider_configs_survive_restart_and_drive_their_exact_executors(
    tmp_path: Path,
) -> None:
    first_factories = (
        RecordingAgentExecutorFactoryV2(
            "openai", "codex-cli/v1", "codex-before-restart", b"\xffreview"
        ),
        RecordingAgentExecutorFactoryV2(
            "anthropic", "claude-cli/v1", "claude-before-restart", b"build"
        ),
    )
    first = _runtime(tmp_path, first_factories)
    first.initialize_storage()
    workflow, bindings = _publish_matrix(first)
    run_id = RunId("provider-neutral/restart")
    result = DbosDurableRunStarter(
        first.engine, first.settings, first.agent_executor_registry
    ).start_published(
        StartPublishedRunRequestV2(run_id, workflow.revision_hash, bindings)
    )
    assert isinstance(result, DurableRunCreated)
    first.close()

    restarted_factories = (
        RecordingAgentExecutorFactoryV2(
            "anthropic", "claude-cli/v1", "claude-after-restart", b"build"
        ),
        RecordingAgentExecutorFactoryV2(
            "openai", "codex-cli/v1", "codex-after-restart", b"\xffreview"
        ),
    )
    restarted = _runtime(tmp_path, restarted_factories)
    try:
        restarted.launch()
        completed = _wait_completed(restarted, run_id)
        queried = DbosQueries(restarted.engine)
        found = queried.get_run(run_id)
        page = queried.list_runs(None, 100)
        assert isinstance(found, RunFound)
        assert isinstance(found.projection.run, RunV2)
        assert isinstance(page, RunPage)
        assert page.runs == (found.projection,)
        assert completed.binding_set_hash == bindings.binding_set_hash
        assert tuple(binding.role.value for binding in completed.agent_bindings) == (
            "builder",
            "reviewer",
        )
        with restarted.engine.connect() as connection:
            assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 1
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(run_agent_bindings)
                )
                == 2
            )
            receipts = tuple(
                connection.execute(
                    sa.select(agent_receipts_v2).order_by(agent_receipts_v2.c.role)
                ).mappings()
            )
        assert [record["executor_operational_identity"] for record in receipts] == [
            "claude-after-restart",
            "codex-after-restart",
        ]
        assert bytes(receipts[1]["output_bytes"]) == b"\xffreview"
    finally:
        restarted.close()


def test_private_factory_canary_never_enters_any_public_or_durable_channel(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    canary = "private-provider-material-7f7b0d8b"
    factory = RecordingAgentExecutorFactoryV2(
        "anthropic", "claude-cli/v1", "public-operation", b"built"
    )
    factory.__dict__["private_material"] = canary
    runtime = _runtime(tmp_path, (factory,))
    runtime.initialize_storage()
    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    auth = AuthProfileRevision("max", 1, ProviderId("anthropic"), AuthMode.SUBSCRIPTION)
    assert isinstance(
        catalog.publish_auth_profile_revision(auth), AuthProfileRevisionCreated
    )
    configuration = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision("claude-cli/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    assert isinstance(
        catalog.publish_agent_configuration_revision(configuration),
        AgentConfigurationRevisionCreated,
    )
    document = b"""format_version: 2
start: build
nodes:
  - {id: done, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: build, type: agent, role: builder, job: build, next: done}
"""
    workflow = WorkflowRevision(document)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    run_id = RunId("secret-free/channels")
    bindings = AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )
    started = DbosDurableRunStarter(
        runtime.engine, runtime.settings, runtime.agent_executor_registry
    ).start_published(
        StartPublishedRunRequestV2(run_id, workflow.revision_hash, bindings)
    )
    assert isinstance(started, DurableRunCreated)

    try:
        runtime.launch()
        _wait_completed(runtime, run_id)
        client = _api_client(runtime)
        public_reference = encode_public_run_reference(run_id)
        public_channels = (
            client.get(API_PREFIX + f"/runs/{public_reference}").text,
            client.get(API_PREFIX + "/runs").text,
            client.get(API_PREFIX + f"/runs/{public_reference}/events").text,
            str(cast(FastAPI, client.app).openapi()),
        )
        assert all(canary not in channel for channel in public_channels)

        with runtime.engine.connect() as connection:
            table_names = tuple(sa.inspect(connection).get_table_names())
            assert {
                "runs",
                "run_events",
                "agent_receipts_v2",
                "reconcile_commands",
                "workflow_status",
                "operation_outputs",
            }.issubset(table_names)
            durable_channels = {
                table_name: tuple(
                    tuple(row)
                    for row in connection.exec_driver_sql(
                        f'SELECT * FROM "{table_name}"'
                    )
                )
                for table_name in table_names
            }
        assert len(durable_channels["agent_receipts_v2"]) == 1
        assert len(durable_channels["run_events"]) == 2
        assert durable_channels["workflow_status"]
        assert durable_channels["operation_outputs"]
        assert all(canary not in repr(rows) for rows in durable_channels.values())
        assert canary not in "\n".join(record.getMessage() for record in caplog.records)
    finally:
        runtime.close()


def test_catalog_workflow_start_get_and_list_roundtrip_through_the_real_api(
    tmp_path: Path,
) -> None:
    factory = RecordingAgentExecutorFactoryV2(
        "anthropic", "claude-cli/v1", "claude-api-test", b"build"
    )
    runtime = _runtime(tmp_path, (factory,))
    runtime.initialize_storage()
    client = _api_client(runtime)
    try:
        auth = client.post(
            API_PREFIX + "/auth-profile-revisions",
            json={
                "profile_id": "max",
                "revision_number": 1,
                "provider_id": "anthropic",
                "auth_mode": "subscription",
            },
        )
        assert auth.status_code == 201
        auth_hash = auth.json()["auth_profile_revision_hash"]
        configuration = client.post(
            API_PREFIX + "/agent-configuration-revisions",
            json={
                "model": "opus",
                "auth_profile_revision_hash": auth_hash,
                "executor_revision": "claude-cli/v1",
            },
        )
        assert configuration.status_code == 201
        configuration_hash = configuration.json()["agent_configuration_revision_hash"]
        workflow = client.post(
            API_PREFIX + "/workflow-revisions",
            content=b"""format_version: 2
start: build
nodes:
  - {id: done, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: build, type: agent, role: builder, job: build, next: done}
""",
            headers={"content-type": "application/yaml"},
        )
        assert workflow.status_code == 201
        revision_hash = workflow.json()["revision_hash"]
        request = {
            "workflow_format_version": 2,
            "run_id": "api/v2",
            "workflow_revision_hash": revision_hash,
            "agent_bindings": [
                {
                    "role": "builder",
                    "agent_configuration_revision_hash": configuration_hash,
                }
            ],
        }

        created = client.post(API_PREFIX + "/runs", json=request)
        retry = client.post(API_PREFIX + "/runs", json=request)
        found = client.get(
            API_PREFIX + "/runs/" + created.json()["public_run_reference"]
        )
        page = client.get(API_PREFIX + "/runs")

        assert created.status_code == 201
        assert retry.status_code == 200
        assert found.status_code == 200
        assert page.status_code == 200
        assert created.json() == retry.json() == found.json() == page.json()["items"][0]
        assert created.json()["workflow_format_version"] == 2
        assert created.json()["agent_bindings"] == [
            {
                "role": "builder",
                "agent_configuration_revision_hash": configuration_hash,
                "auth_profile_revision_hash": auth_hash,
                "profile_id": "max",
                "revision_number": 1,
                "provider_id": "anthropic",
                "auth_mode": "subscription",
                "model": "opus",
                "executor_revision": "claude-cli/v1",
            }
        ]
        with runtime.engine.connect() as connection:
            assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 1
    finally:
        runtime.close()


def test_numbered_profile_and_configuration_revisions_are_immutable_and_restart(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog-restart"
    factory = RecordingAgentExecutorFactoryV2(
        "anthropic", "claude-cli/v1", "catalog-before", b""
    )
    first = _runtime(root, (factory,))
    first.initialize_storage()
    client = _api_client(first)

    def publish_auth(revision_number: int, auth_mode: str = "subscription") -> Response:
        return client.post(
            API_PREFIX + "/auth-profile-revisions",
            json={
                "profile_id": "max",
                "revision_number": revision_number,
                "provider_id": "anthropic",
                "auth_mode": auth_mode,
            },
        )

    first_revision = publish_auth(1)
    exact_retry = publish_auth(1)
    second_revision = publish_auth(2)
    conflict = publish_auth(1, "api_key")
    configuration_request = {
        "model": "opus",
        "auth_profile_revision_hash": first_revision.json()[
            "auth_profile_revision_hash"
        ],
        "executor_revision": "claude-cli/v1",
    }
    configuration = client.post(
        API_PREFIX + "/agent-configuration-revisions", json=configuration_request
    )
    configuration_retry = client.post(
        API_PREFIX + "/agent-configuration-revisions", json=configuration_request
    )

    assert [
        first_revision.status_code,
        exact_retry.status_code,
        second_revision.status_code,
        conflict.status_code,
        configuration.status_code,
        configuration_retry.status_code,
    ] == [201, 200, 201, 409, 201, 200]
    assert conflict.json()["type"].endswith(":auth-profile-revision-conflict")
    first.close()

    restarted = _runtime(
        root,
        (
            RecordingAgentExecutorFactoryV2(
                "anthropic", "claude-cli/v1", "catalog-after", b""
            ),
        ),
    )
    try:
        restarted_client = _api_client(restarted)
        assert (
            restarted_client.post(
                API_PREFIX + "/auth-profile-revisions",
                json={
                    "profile_id": "max",
                    "revision_number": 1,
                    "provider_id": "anthropic",
                    "auth_mode": "subscription",
                },
            ).status_code
            == 200
        )
        assert (
            restarted_client.post(
                API_PREFIX + "/agent-configuration-revisions",
                json=configuration_request,
            ).status_code
            == 200
        )
        with restarted.engine.connect() as connection:
            assert (
                connection.scalar(
                    sa.text("SELECT COUNT(*) FROM auth_profile_revisions")
                )
                == 2
            )
            assert (
                connection.scalar(
                    sa.text("SELECT COUNT(*) FROM agent_configuration_revisions")
                )
                == 1
            )
    finally:
        restarted.close()


def test_v2_start_refusals_precede_run_queue_event_and_rebind_mutation(
    tmp_path: Path,
) -> None:
    factory = RecordingAgentExecutorFactoryV2(
        "anthropic", "claude-cli/v1", "claude-refusal", b"build"
    )
    runtime = _runtime(tmp_path, (factory,))
    runtime.initialize_storage()
    client = _api_client(runtime)
    auth = client.post(
        API_PREFIX + "/auth-profile-revisions",
        json={
            "profile_id": "max",
            "revision_number": 1,
            "provider_id": "anthropic",
            "auth_mode": "subscription",
        },
    ).json()
    configuration_hashes = []
    for model in ("opus", "sonnet"):
        response = client.post(
            API_PREFIX + "/agent-configuration-revisions",
            json={
                "model": model,
                "auth_profile_revision_hash": auth["auth_profile_revision_hash"],
                "executor_revision": "claude-cli/v1",
            },
        )
        assert response.status_code == 201
        configuration_hashes.append(
            response.json()["agent_configuration_revision_hash"]
        )
    workflow = client.post(
        API_PREFIX + "/workflow-revisions",
        content=b"""format_version: 2
start: build
nodes:
  - {id: done, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: build, type: agent, role: builder, job: build, next: done}
""",
        headers={"content-type": "application/yaml"},
    ).json()
    revision_hash = workflow["revision_hash"]

    def start(run_id: str, role: str, configuration_hash: str) -> Response:
        return client.post(
            API_PREFIX + "/runs",
            json={
                "workflow_format_version": 2,
                "run_id": run_id,
                "workflow_revision_hash": revision_hash,
                "agent_bindings": [
                    {
                        "role": role,
                        "agent_configuration_revision_hash": configuration_hash,
                    }
                ],
            },
        )

    try:
        missing = start("missing", "builder", "f" * 64)
        wrong_role = start("wrong-role", "reviewer", configuration_hashes[0])
        valid = start("identity", "builder", configuration_hashes[0])
        rebound = start("identity", "builder", configuration_hashes[1])
        rebound_to_missing = start("identity", "builder", "f" * 64)
        version_mismatch = client.post(
            API_PREFIX + "/runs",
            json={
                "run_id": "v1-request-v2-graph",
                "workflow_revision_hash": revision_hash,
            },
        )

        assert missing.status_code == 404
        assert missing.json()["type"].endswith(
            ":agent-configuration-revision-not-found"
        )
        assert wrong_role.status_code == 422
        assert wrong_role.json()["type"].endswith(":invalid-agent-bindings")
        assert valid.status_code == 201
        assert rebound.status_code == 409
        assert rebound.json()["type"].endswith(":run-identity-conflict")
        assert rebound_to_missing.status_code == 409
        assert rebound_to_missing.json()["type"].endswith(":run-identity-conflict")
        assert version_mismatch.status_code == 422
        assert version_mismatch.json()["type"].endswith(":invalid-agent-bindings")
        with runtime.engine.connect() as connection:
            assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 1
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(run_agent_bindings)
                )
                == 1
            )
            assert (
                connection.scalar(
                    sa.select(run_agent_bindings.c.agent_configuration_revision_hash)
                )
                == configuration_hashes[0]
            )
            assert (
                connection.scalar(sa.select(sa.func.count()).select_from(run_events))
                == 0
            )
    finally:
        runtime.close()

    empty_root = tmp_path / "production-empty"
    seeded = _runtime(
        empty_root,
        (
            RecordingAgentExecutorFactoryV2(
                "anthropic", "claude-cli/v1", "seed-only", b"build"
            ),
        ),
    )
    seeded.initialize_storage()
    seeded_client = _api_client(seeded)
    seeded_auth = seeded_client.post(
        API_PREFIX + "/auth-profile-revisions",
        json={
            "profile_id": "max",
            "revision_number": 1,
            "provider_id": "anthropic",
            "auth_mode": "subscription",
        },
    ).json()
    seeded_configuration = seeded_client.post(
        API_PREFIX + "/agent-configuration-revisions",
        json={
            "model": "opus",
            "auth_profile_revision_hash": seeded_auth["auth_profile_revision_hash"],
            "executor_revision": "claude-cli/v1",
        },
    ).json()["agent_configuration_revision_hash"]
    seeded_revision = seeded_client.post(
        API_PREFIX + "/workflow-revisions",
        content=b"""format_version: 2
start: build
nodes:
  - {id: done, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: build, type: agent, role: builder, job: build, next: done}
""",
        headers={"content-type": "application/yaml"},
    ).json()["revision_hash"]
    seeded.close()

    production_empty = _runtime(empty_root, ())
    try:
        unavailable = _api_client(production_empty).post(
            API_PREFIX + "/runs",
            json={
                "workflow_format_version": 2,
                "run_id": "no-production-executor",
                "workflow_revision_hash": seeded_revision,
                "agent_bindings": [
                    {
                        "role": "builder",
                        "agent_configuration_revision_hash": seeded_configuration,
                    }
                ],
            },
        )
        assert unavailable.status_code == 409
        assert unavailable.json()["type"].endswith(
            ":agent-executor-binding-unavailable"
        )
        with production_empty.engine.connect() as connection:
            assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 0
            assert (
                connection.scalar(sa.select(sa.func.count()).select_from(run_events))
                == 0
            )
    finally:
        production_empty.close()


@pytest.mark.parametrize(("output_size", "accepted"), [(49_152, True), (49_153, False)])
def test_v2_output_bound_is_checked_before_atomic_receipt_event_and_run_cas(
    tmp_path: Path, output_size: int, accepted: bool
) -> None:
    factories = (
        RecordingAgentExecutorFactoryV2(
            "anthropic", "claude-cli/v1", "claude-bound", b"build"
        ),
        RecordingAgentExecutorFactoryV2(
            "openai", "codex-cli/v1", "codex-bound", b"review"
        ),
    )
    runtime = _runtime(tmp_path, factories)
    runtime.initialize_storage()
    try:
        workflow, bindings = _publish_matrix(runtime)
        run_id = RunId(f"output/{output_size}")
        started = DbosDurableRunStarter(
            runtime.engine, runtime.settings, runtime.agent_executor_registry
        ).start_published(
            StartPublishedRunRequestV2(run_id, workflow.revision_hash, bindings)
        )
        assert isinstance(started, DurableRunCreated)
        assert isinstance(started.run, RunV2)
        resolved = next(
            binding
            for binding in started.run.agent_bindings
            if binding.role.value == "builder"
        )
        request = AgentExecutionRequestV2(
            NodeExecutionId.for_node(run_id, workflow.revision_hash, "build"),
            run_id,
            workflow.revision_hash,
            "build",
            resolved,
            AgentExecutorOperationalIdentity("claude-bound"),
            b"build",
        )

        if accepted:
            store = DbosAgentAttemptStore(runtime.engine)
            store.prepare(agent_attempt_execution(request))
            store.claim(agent_attempt_execution(request))
            snapshot = store.complete_success(
                agent_attempt_execution(request),
                AgentExecutionResult(b"x" * output_size),
            )
            retried = store.claim(agent_attempt_execution(request))
            assert snapshot.attempt.state_version == 2
            assert retried == snapshot
            expected_receipts = expected_events = 1
        else:
            store = DbosAgentAttemptStore(runtime.engine)
            store.prepare(agent_attempt_execution(request))
            store.claim(agent_attempt_execution(request))
            with pytest.raises(AgentOutputLimitExceeded):
                store.complete_success(
                    agent_attempt_execution(request),
                    AgentExecutionResult(b"x" * output_size),
                )
            expected_receipts = expected_events = 0

        with runtime.engine.connect() as connection:
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(agent_receipts_v2)
                )
                == expected_receipts
            )
            assert (
                connection.scalar(sa.select(sa.func.count()).select_from(run_events))
                == expected_events
            )
            record = connection.execute(
                sa.select(
                    runs.c.current_node_id,
                    runs.c.state_version,
                    runs.c.last_event_sequence,
                ).where(runs.c.run_id == run_id.value)
            ).one()
        assert tuple(record) == (("review", 1, 1) if accepted else ("build", 0, 0))
    finally:
        runtime.close()
