from __future__ import annotations

import pytest

from atelier2.adapters.yaml_workflows import parse_executable_workflow_document
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
)
from atelier2.contracts.run_bindings import RunV3
from atelier2.contracts.run_configuration_v3 import RunConfigurationRevisionHash
from atelier2.contracts.run_projections import (
    AgentAttemptProjection,
    NodeState,
    PublicAgentAttemptState,
    RunProjection,
)
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
from tests.scenarios.api import api_limits

SERVED_RAIL = (
    NodeRailResource(node_id="build", state=NodeState.WORKING, attempt=None),
    NodeRailResource(node_id="done", state=NodeState.QUEUED, attempt=None),
)
"""What the stream folds onto an event; this file proves the shape, not the fold."""


def run_projection(
    state: PublicAgentAttemptState, failure: bool = False
) -> RunProjection:
    document = b"""format_version: 3
name: Build a candidate, then check it
nodes:
  - id: build
    type: agent
    role: builder
    mode: headless
    instruction: Build the candidate this run was started for.
    outputs:
      - name: candidate
        schema: {ref: workspace_candidate, revision: schema-candidate}
  - id: done
    type: agent
    role: builder
    mode: headless
    instruction: Check the candidate the previous node built.
    depends_on: [build]
    inputs:
      - name: candidate
        from: {node: build, output: candidate}
    outputs:
      - name: findings
        schema: {ref: review_verdict, revision: schema-verdict}
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
        RunV3(
            run_id,
            workflow.revision_hash,
            binding_set.binding_set_hash,
            (binding,),
            RunState.STARTED,
            "build",
            0,
            0,
            RunConfigurationRevisionHash("c" * 64),
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
    projection = run_projection(PublicAgentAttemptState.POSSIBLY_RAN)

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
    """The rail names the attempt a reader is told about, and nothing private.

    The run resource carries no `agent_attempts` list: the rail's attempt is the
    one owner of that fact, so this reads it where the reader meets it.
    """

    projection = run_projection(PublicAgentAttemptState.POSSIBLY_RAN)

    resource = run_resource(projection)
    api_limits().require_run_projection(projection)
    payload = resource.model_dump(mode="json")

    assert payload["node_rail"][0]["attempt"] == {
        "ordinal": 1,
        "state": "POSSIBLY_RAN",
    }
    assert "agent_attempts" not in payload
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
