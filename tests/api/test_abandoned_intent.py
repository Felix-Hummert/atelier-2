"""ABANDONED on GET /runs, GET /runs/{ref}, and the node detail.

Hop 38 made ABANDONED durable. The query projection already attaches the
intent to an ended Action run. A dedicated run-resource field would name
that word beside the run state, but the wire schema is claimed elsewhere;
the existing node-detail `refusal` is the string that can carry it today.
GET /runs names the run's own ending as the reason.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from atelier2.api.app import create_app
from atelier2.api.projection.runs import node_detail_resource, run_resource
from atelier2.api.references import encode_public_run_reference
from atelier2.api.wire.resources import RunResourceV3
from atelier2.contracts.agents import AgentBindingSet
from atelier2.contracts.effects import (
    EFFECT_INTENT_VERSION_ABANDONED,
    AdapterOperationalIdentity,
    AdapterRevision,
    CanonicalRequest,
    EffectBinding,
    EffectDestination,
    EffectIntent,
    EffectIntentSnapshot,
    EffectIntentState,
    LogicalEffectKey,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.run_bindings import RunV3
from atelier2.contracts.run_configuration_v3 import RunConfigurationRevisionHash
from atelier2.contracts.run_projections import (
    NodeDetail,
    NodeState,
    RunPage,
    RunProjection,
    WaitingReconciliationProjection,
)
from atelier2.contracts.runs import RunId, RunState, WorkflowRevisionHash
from atelier2.contracts.workflows_v3 import (
    ActionNodeV3,
    VersionedReference,
    WorkflowGraphV3,
)
from atelier2.ports.run_queries import (
    GetReconciliationRetryTargetResult,
    GetRunResult,
    ListRunsResult,
    NodeDetailFound,
    RunFound,
)
from atelier2.ports.workflow_revisions import DurableProjectionLimit
from tests.scenarios.api import api_limits, api_ports, event_poll_backoff

RUN_ID = RunId("run-abandoned")
REVISION_HASH = WorkflowRevisionHash("a" * 64)
NODE_ID = "action"
REQUEST = CanonicalRequest(b"request")
PUBLIC_REF = encode_public_run_reference(RUN_ID)
INTENT = EffectIntent(
    EffectBinding(
        LogicalEffectKey("abandoned-effect"),
        RUN_ID,
        REVISION_HASH,
        AdapterRevision("loopback-v1"),
        EffectDestination("loopback-test"),
        AdapterOperationalIdentity("loopback-operation"),
    ),
    REQUEST,
)


def _graph() -> WorkflowGraphV3:
    return WorkflowGraphV3(
        format_version=3,
        name="An action whose prepared effect was abandoned",
        nodes=(
            ActionNodeV3(
                id=NODE_ID,
                type="action",
                operation=VersionedReference(
                    ref="loopback.perform", revision="loopback-v1"
                ),
            ),
        ),
    )


def _projection(ending: RunState) -> RunProjection:
    return RunProjection(
        RunV3(
            RUN_ID,
            REVISION_HASH,
            AgentBindingSet(()).binding_set_hash,
            (),
            ending,
            NODE_ID,
            1,
            1,
            RunConfigurationRevisionHash("c" * 64),
            Sha256Hash.of(b"ended"),
        ),
        _graph(),
        WaitingReconciliationProjection(
            EffectIntentSnapshot(
                INTENT, EffectIntentState.ABANDONED, EFFECT_INTENT_VERSION_ABANDONED
            ),
            None,
        ),
    )


def _node_state(ending: RunState) -> NodeState:
    return NodeState.FAILED if ending is RunState.FAILED else NodeState.CANCELLED


def _detail(ending: RunState) -> NodeDetail:
    return NodeDetail(
        RUN_ID,
        NODE_ID,
        _node_state(ending),
        None,
        None,
        None,
        None,
        EffectIntentState.ABANDONED.value,
    )


class _RunQueries:
    def __init__(self, ending: RunState) -> None:
        self._ending = ending

    def get_run(
        self,
        run_id: object,
        projection_limit: DurableProjectionLimit | None = None,
    ) -> GetRunResult:
        del run_id, projection_limit
        return RunFound(_projection(self._ending))

    def get_node_detail(self, run_id: object, node_id: str) -> NodeDetailFound:
        del run_id
        if node_id != NODE_ID:
            raise AssertionError(
                "node detail asked for a node this run does not stand on"
            )
        return NodeDetailFound(_detail(self._ending))

    def list_runs(
        self,
        after: object,
        limit: int,
        state: object = None,
        projection_limit: DurableProjectionLimit | None = None,
    ) -> ListRunsResult:
        del after, limit, state, projection_limit
        return RunPage((_projection(self._ending),), None)

    def get_reconciliation_retry_target(
        self,
        run_id: object,
        command_id: object,
        projection_limit: DurableProjectionLimit | None = None,
    ) -> GetReconciliationRetryTargetResult:
        del run_id, command_id, projection_limit
        raise AssertionError("abandoned-intent read must not query reconciliation")


def _client(ending: RunState) -> TestClient:
    return TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=api_ports(run_queries=_RunQueries(ending)),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        )
    )


@pytest.mark.parametrize("ending", [RunState.FAILED, RunState.CANCELLED])
def test_get_run_and_node_detail_name_abandoned_with_the_run_ending(
    ending: RunState,
) -> None:
    """GET /runs names the ending; the node detail names ABANDONED as its refusal."""

    client = _client(ending)
    listed = client.get("/atelier/api/v1/runs")
    run = client.get(f"/atelier/api/v1/runs/{PUBLIC_REF}")
    detail = client.get(f"/atelier/api/v1/runs/{PUBLIC_REF}/nodes/{NODE_ID}")

    assert listed.status_code == 200
    assert run.status_code == 200
    assert listed.json()["items"] == [run.json()]
    listed_run = listed.json()["items"][0]
    assert listed_run["state"] == ending.value
    assert listed_run["current_node_id"] == NODE_ID
    assert listed_run["node_rail"][0]["state"] == _node_state(ending).value
    assert listed_run["terminal_hash"] is not None
    assert detail.status_code == 200
    assert detail.json()["refusal"] == EffectIntentState.ABANDONED.value
    assert detail.json()["state"] == listed_run["node_rail"][0]["state"]
    assert detail.json()["node_id"] == listed_run["current_node_id"]
    assert detail.json()["run_id"] == listed_run["run_id"]


@pytest.mark.parametrize("ending", [RunState.FAILED, RunState.CANCELLED])
def test_an_abandoned_intent_stays_on_the_ended_run_resource(
    ending: RunState,
) -> None:
    resource = run_resource(_projection(ending))
    rendered = node_detail_resource(_detail(ending))

    assert isinstance(resource, RunResourceV3)
    assert resource.state == ending.value
    assert resource.current_node_id == NODE_ID
    assert rendered.refusal == EffectIntentState.ABANDONED.value
    assert rendered.state == _node_state(ending).value
