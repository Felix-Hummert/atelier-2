from __future__ import annotations

import pytest

from atelier2.adapters.yaml_workflows import parse_executable_workflow_document
from atelier2.api.projection.events import run_event_resource
from atelier2.api.projection.runs import run_resource
from atelier2.api.wire.resources import NodeRailResource
from atelier2.contracts.agent_attempts import (
    AgentAttemptFailureCode,
    AgentAttemptId,
)
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutionRequestHash,
    AgentExecutorRevision,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
    ResolvedAgentBinding,
)
from atelier2.contracts.executions import (
    NodeExecutionId,
    RunEvent,
    RunEventAgentAttemptBinding,
    RunEventKind,
)
from atelier2.contracts.run_bindings import RunV2
from atelier2.contracts.run_events import (
    PersistedRunEvent,
)
from atelier2.contracts.run_projections import (
    AgentAttemptProjection,
    NodeState,
    PublicAgentAttemptState,
    RunProjection,
)
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
from atelier2.contracts.workflow_formats import WorkflowFormatVersion
from tests.scenarios.api import api_limits

SERVED_RAIL = (
    NodeRailResource(node_id="build", state=NodeState.WORKING, attempt=None),
    NodeRailResource(node_id="done", state=NodeState.QUEUED, attempt=None),
)
"""What the stream folds onto an event; this file proves the shape, not the fold."""


def v2_run_projection(
    state: PublicAgentAttemptState, failure: bool = False
) -> RunProjection:
    document = b"""format_version: 2
start: build
nodes:
  - {id: done, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: build, type: agent, role: builder, job: build, next: done}
"""
    workflow = WorkflowRevision(document)
    graph = parse_executable_workflow_document(document)
    auth = AuthProfileRevision("max", 1, ProviderId("anthropic"), AuthMode.SUBSCRIPTION)
    configuration = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision("claude-cli/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    binding = ResolvedAgentBinding(AgentRole("builder"), configuration, auth)
    binding_set = AgentBindingSet(
        (AgentBinding(binding.role, configuration.revision_hash),)
    )
    run_id = RunId("attempt/api")
    execution_id = NodeExecutionId.for_node(run_id, workflow.revision_hash, "build")
    request_hash = AgentExecutionRequestHash("1" * 64)
    return RunProjection(
        RunV2(
            run_id,
            workflow.revision_hash,
            binding_set.binding_set_hash,
            (binding,),
            RunState.STARTED,
            "build",
            0,
            0,
        ),
        graph,
        None,
        (
            AgentAttemptProjection(
                AgentAttemptId.for_execution(execution_id, request_hash),
                execution_id,
                request_hash,
                1,
                state,
                (
                    AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY
                    if failure
                    else None
                ),
            ),
        ),
    )


@pytest.mark.proves("the-run-resource-names-the-state-of-every-node")
def test_the_served_v2_run_names_the_state_of_every_node_of_its_revision() -> None:
    projection = v2_run_projection(PublicAgentAttemptState.POSSIBLY_RAN)

    payload = run_resource(projection).model_dump(mode="json")

    assert payload["node_rail"] == [
        {
            "node_id": "build",
            "state": "working",
            "attempt": {"ordinal": 1, "state": "POSSIBLY_RAN"},
        },
        {"node_id": "done", "state": "queued", "attempt": None},
    ]


def test_attempt_surfaces_are_canonical_bounded_and_secret_free() -> None:
    projection = v2_run_projection(PublicAgentAttemptState.POSSIBLY_RAN)

    resource = run_resource(projection)
    api_limits().require_run_projection(projection)
    payload = resource.model_dump(mode="json")
    attempt = projection.current_agent_attempt
    assert attempt is not None

    assert payload["agent_attempts"] == [
        {
            "attempt_id": attempt.attempt_id.value,
            "node_execution_id": attempt.node_execution_id.value,
            "request_hash": "1" * 64,
            "attempt_ordinal": 1,
            "state": "POSSIBLY_RAN",
            "failure_code": None,
            "cancellation": None,
        }
    ]
    assert all(
        forbidden not in repr(payload).lower()
        for forbidden in (
            "secret",
            "credential",
            "environment",
            "stderr",
            "pid",
            "path",
        )
    )


def test_v2_attempt_and_failed_event_have_exact_wire_shape() -> None:
    projection = v2_run_projection(PublicAgentAttemptState.FAILED, failure=True)
    attempt_resource = run_resource(projection).model_dump(mode="json")[
        "agent_attempts"
    ][0]
    attempt = projection.current_agent_attempt
    assert attempt is not None
    run = projection.run
    event = RunEvent(
        run.run_id,
        run.revision_hash,
        1,
        "build",
        attempt.node_execution_id,
        RunEventKind.AGENT_FAILED,
        b"PROCESS_EXITED_UNSUCCESSFULLY",
        attempt_binding=RunEventAgentAttemptBinding(attempt.attempt_id, 1),
    )
    persisted = PersistedRunEvent(event, None, WorkflowFormatVersion.V2)
    event_resource = run_event_resource(persisted, SERVED_RAIL)
    api_limits().require_event_projection(persisted)

    assert attempt_resource["state"] == "FAILED"
    assert attempt_resource["failure_code"] == "PROCESS_EXITED_UNSUCCESSFULLY"
    assert event_resource.model_dump(mode="json") == {
        "workflow_format_version": 2,
        "node_rail": [
            {"node_id": "build", "state": "working", "attempt": None},
            {"node_id": "done", "state": "queued", "attempt": None},
        ],
        "cursor": "event1.YXR0ZW1wdC9hcGk.1",
        "sequence": 1,
        "public_run_reference": "run1.YXR0ZW1wdC9hcGk",
        "workflow_revision_hash": run.revision_hash.value,
        "node_id": "build",
        "node_execution_id": event.node_execution_id.value,
        "event_hash": event.event_hash.value,
        "event": "AGENT_FAILED",
        "failure_code": "PROCESS_EXITED_UNSUCCESSFULLY",
        "attempt_id": attempt.attempt_id.value,
        "attempt_ordinal": 1,
    }
