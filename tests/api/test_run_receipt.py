from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from atelier2.api.app import create_app
from atelier2.api.openapi import API_PREFIX
from atelier2.api.references import encode_canonical_base64, encode_public_run_reference
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
    AgentReceiptV2,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
    ResolvedAgentBinding,
)
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.ports.run_queries import RunQueryMissing, RunReceiptsFound
from atelier2.ports.workflow_revisions import QueryDurableStateCorrupt, ReadUnavailable
from tests.scenarios.api import api_limits, api_ports, event_poll_backoff

AUTH = AuthProfileRevision("max", 7, ProviderId("anthropic"), AuthMode.SUBSCRIPTION)
CONFIGURATION = AgentConfigurationRevision(
    "claude-opus-4-1",
    AUTH.revision_hash,
    AgentExecutorRevision("claude-cli/v1"),
    AgentExecutionCapability.HEADLESS,
    AgentConfigurationRevisionFormatVersion.V2,
)
RUN_ID = RunId("run/receipt")
REVISION_HASH = WorkflowRevisionHash("1" * 64)
OUTPUT = b'{"ok": true}'


def _receipt() -> AgentReceiptV2:
    resolved = ResolvedAgentBinding(AgentRole("builder"), CONFIGURATION, AUTH)
    bindings = AgentBindingSet(
        (AgentBinding(resolved.role, CONFIGURATION.revision_hash),)
    )
    request = AgentExecutionRequestV2(
        NodeExecutionId.for_node(RUN_ID, REVISION_HASH, "implement"),
        RUN_ID,
        REVISION_HASH,
        "implement",
        resolved,
        AgentExecutorOperationalIdentity("exact-op"),
        b"write the draft",
    )
    return AgentReceiptV2.for_execution(
        request, bindings.binding_set_hash, AgentExecutionResult(OUTPUT)
    )


@dataclass
class RecordingReceiptQueries:
    result: object
    asked: list[object] | None = None

    def __post_init__(self) -> None:
        if self.asked is None:
            self.asked = []

    def list_run_receipts(self, run_id: object) -> object:
        assert self.asked is not None
        self.asked.append(run_id)
        return self.result


def _client(queries: RecordingReceiptQueries) -> TestClient:
    return TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=api_ports(run_queries=queries),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        )
    )


def test_receipt_answers_with_every_binding_dimension_and_no_secrets() -> None:
    receipt = _receipt()
    queries = RecordingReceiptQueries(RunReceiptsFound((receipt,)))

    response = _client(queries).get(
        API_PREFIX + "/runs/" + encode_public_run_reference(RUN_ID) + "/receipt"
    )

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "request_hash": receipt.request_hash.value,
                "node_execution_id": receipt.node_execution_id.value,
                "run_id": RUN_ID.value,
                "workflow_revision_hash": REVISION_HASH.value,
                "node_id": "implement",
                "role": "builder",
                "binding_set_hash": receipt.binding_set_hash.value,
                "agent_configuration_revision_hash": (
                    CONFIGURATION.revision_hash.value
                ),
                "auth_profile_revision_hash": AUTH.revision_hash.value,
                "profile_id": "max",
                "revision_number": 7,
                "provider_id": "anthropic",
                "auth_mode": "subscription",
                "model": "claude-opus-4-1",
                "executor_revision": "claude-cli/v1",
                "executor_operational_identity": "exact-op",
                "output_base64": encode_canonical_base64(OUTPUT),
                "output_hash": receipt.output_hash.value,
                "receipt_hash": receipt.receipt_hash.value,
            }
        ]
    }
    assert all(
        forbidden not in response.text.lower()
        for forbidden in ("secret", "credential", "handle", "api_key_value")
    )
    assert queries.asked == [RUN_ID]


def test_receipt_of_a_run_that_has_written_none_is_an_empty_list() -> None:
    queries = RecordingReceiptQueries(RunReceiptsFound(()))

    response = _client(queries).get(
        API_PREFIX + "/runs/" + encode_public_run_reference(RUN_ID) + "/receipt"
    )

    assert response.status_code == 200
    assert response.json() == {"items": []}
    assert queries.asked == [RUN_ID]


def test_receipt_of_a_missing_run_is_named() -> None:
    queries = RecordingReceiptQueries(RunQueryMissing())

    response = _client(queries).get(
        API_PREFIX + "/runs/" + encode_public_run_reference(RUN_ID) + "/receipt"
    )

    assert response.status_code == 404
    assert response.json()["type"].endswith(":run-not-found")
    assert queries.asked == [RUN_ID]


@pytest.mark.parametrize(
    ("result", "status", "code"),
    [
        (ReadUnavailable("store asleep"), 503, "temporarily-unavailable"),
        (QueryDurableStateCorrupt(), 500, "durable-state-corrupt"),
    ],
)
def test_receipt_maps_every_read_refusal(
    result: object, status: int, code: str
) -> None:
    response = _client(RecordingReceiptQueries(result)).get(
        API_PREFIX + "/runs/" + encode_public_run_reference(RUN_ID) + "/receipt"
    )

    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"] == f"urn:atelier2:problem:v1:{code}"
