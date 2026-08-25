from __future__ import annotations

from typing import Literal

import pytest
from pydantic import TypeAdapter, ValidationError

from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.api.projection.workflows import graph_resource
from atelier2.api.wire.events import AgentCompletedEventResource, RunEventResource
from atelier2.api.wire.resources import (
    ActionNodeResource,
    AgentAttemptResourceV2,
    AgentConfigurationRevisionListItemResource,
    AgentNodeResource,
    NoWaitingResource,
    RunResource,
    SubworkflowNodeResource,
    WaitAnswerSchemaResourceV3,
    WaitingInputResource,
    WaitingReconciliationResource,
    WaitNodeResource,
    WorkflowDeclaredSchemaResourceV3,
    WorkflowGraphResourceV3,
    WorkflowLoopVerdictResourceV3,
    WorkflowNodePreviewResourceV3,
)
from atelier2.application.read_workflow_revisions import WaitAnswerClassification
from tests.scenarios.workflows import (
    V3_DOCUMENT,
    VERDICT_LOOP_DOCUMENT,
    VERDICT_LOOP_MAXIMUM_ROUNDS,
)

HASH = "0" * 64


def _wait_preview(node_id: str) -> WorkflowNodePreviewResourceV3:
    return WorkflowNodePreviewResourceV3(
        id=node_id, kind="wait", role=None, instruction_start=None, depends_on=()
    )


def _wait_answer_schema(
    node_id: str, kind: Literal["boolean", "enum", "free"] = "free"
) -> WaitAnswerSchemaResourceV3:
    return WaitAnswerSchemaResourceV3(
        node_id=node_id,
        schema=WorkflowDeclaredSchemaResourceV3(
            ref="decision", revision="schema-decision"
        ),
        kind=kind,
        values=("true", "false") if kind == "enum" else None,
    )


EXECUTION = "1" * 64


def test_listed_agent_configuration_requires_an_honest_startability_pair() -> None:
    item = {
        "model": "model",
        "auth_profile_revision_hash": HASH,
        "executor_revision": "executor/v1",
        "provider_id": "provider",
        "auth_mode": "subscription",
        "requested_capability": "headless",
        "agent_configuration_revision_hash": EXECUTION,
    }

    assert AgentConfigurationRevisionListItemResource.model_validate(
        {**item, "startable": True, "not_startable_reason": None}
    ).startable
    assert not AgentConfigurationRevisionListItemResource.model_validate(
        {
            **item,
            "startable": False,
            "not_startable_reason": "agent-executor-binding-unavailable",
        }
    ).startable
    for startable, reason in (
        (True, "agent-executor-binding-unavailable"),
        (False, None),
    ):
        with pytest.raises(ValidationError):
            AgentConfigurationRevisionListItemResource.model_validate(
                {**item, "startable": startable, "not_startable_reason": reason}
            )


def test_agent_attempt_resource_rejects_incongruent_failure_shape() -> None:
    common = {
        "attempt_id": HASH,
        "node_execution_id": EXECUTION,
        "request_hash": "2" * 64,
        "attempt_ordinal": 1,
    }
    with pytest.raises(ValidationError):
        AgentAttemptResourceV2.model_validate(
            {**common, "state": "FAILED", "failure_code": None}
        )
    with pytest.raises(ValidationError):
        AgentAttemptResourceV2.model_validate(
            {
                **common,
                "state": "PREPARED",
                "failure_code": "PROCESS_EXITED_UNSUCCESSFULLY",
            }
        )


@pytest.mark.parametrize(
    "failure_code",
    ("PROCESS_OUTPUT_LIMIT_EXCEEDED", "PROCESS_SUPERVISION_FAILED"),
)
def test_agent_attempt_resource_admits_the_runner_failure_vocabulary(
    failure_code: str,
) -> None:
    resource = AgentAttemptResourceV2.model_validate(
        {
            "attempt_id": HASH,
            "node_execution_id": EXECUTION,
            "request_hash": "2" * 64,
            "attempt_ordinal": 1,
            "state": "FAILED",
            "failure_code": failure_code,
            "cancellation": None,
        }
    )

    assert resource.failure_code == failure_code


def agent_node() -> AgentNodeResource:
    return AgentNodeResource(
        type="agent",
        node_id="agent",
        job="job",
        output="payload",
        next_node_id="action",
    )


def action_node() -> ActionNodeResource:
    return ActionNodeResource(type="action", node_id="action", next_node_id="wait")


def terminal_node() -> SubworkflowNodeResource:
    return SubworkflowNodeResource(
        type="subworkflow",
        node_id="done",
        operation="add",
        operands=(2, 3),
        next_node_id=None,
    )


@pytest.mark.parametrize(
    ("state", "node", "waiting", "terminal_hash"),
    [
        ("STARTED", agent_node(), NoWaitingResource(type="NONE"), None),
        (
            "WAITING_INPUT",
            WaitNodeResource(
                type="wait", node_id="wait", answer_type="integer", next_node_id="done"
            ),
            WaitingInputResource(
                type="WAITING_INPUT", node_id="wait", answer_type="integer"
            ),
            None,
        ),
        (
            "WAITING_RECONCILIATION",
            action_node(),
            WaitingReconciliationResource(
                type="WAITING_RECONCILIATION",
                node_id="action",
                logical_effect_key="effect",
                request_hash=HASH,
                request_base64="cmVxdWVzdA==",
                intent_state_version=1,
                pending_command=None,
            ),
            None,
        ),
        ("COMPLETED", terminal_node(), NoWaitingResource(type="NONE"), HASH),
    ],
)
def test_run_resource_accepts_each_complete_state_shape(
    state: str,
    node: object,
    waiting: object,
    terminal_hash: str | None,
) -> None:
    resource = RunResource.model_validate(
        {
            "run_id": "run",
            "public_run_reference": "run1.cnVu",
            "workflow_revision_hash": HASH,
            "state_version": 2,
            "state": state,
            "current_node": node,
            "waiting": waiting,
            "terminal_hash": terminal_hash,
            "latest_event_cursor": "event1.cnVu.2",
        }
    )

    assert resource.state == state


@pytest.mark.parametrize(
    ("state", "node", "waiting", "terminal_hash"),
    [
        ("STARTED", agent_node(), NoWaitingResource(type="NONE"), HASH),
        ("WAITING_INPUT", agent_node(), NoWaitingResource(type="NONE"), None),
        (
            "WAITING_RECONCILIATION",
            action_node(),
            WaitingInputResource(
                type="WAITING_INPUT", node_id="action", answer_type="integer"
            ),
            None,
        ),
        ("COMPLETED", action_node(), NoWaitingResource(type="NONE"), HASH),
    ],
)
def test_run_resource_rejects_incongruent_state_shapes(
    state: str,
    node: object,
    waiting: object,
    terminal_hash: str | None,
) -> None:
    with pytest.raises(ValidationError):
        RunResource.model_validate(
            {
                "run_id": "run",
                "public_run_reference": "run1.cnVu",
                "workflow_revision_hash": HASH,
                "state_version": 2,
                "state": state,
                "current_node": node,
                "waiting": waiting,
                "terminal_hash": terminal_hash,
                "latest_event_cursor": "event1.cnVu.2",
            }
        )


def _agent_preview(
    node_id: str,
    *,
    role: str = "builder",
    depends_on: tuple[str, ...] = (),
) -> WorkflowNodePreviewResourceV3:
    return WorkflowNodePreviewResourceV3(
        id=node_id,
        kind="agent",
        role=role,
        instruction_start="Do the one thing this chain is for.",
        depends_on=depends_on,
    )


def test_v3_node_preview_carries_the_authored_depends_on() -> None:
    preview = _agent_preview("review", depends_on=("implement",))

    assert preview.depends_on == ("implement",)


def test_v3_graph_accepts_depends_on_that_names_a_sibling_preview() -> None:
    resource = WorkflowGraphResourceV3(
        workflow_format_version=3,
        executable=True,
        not_executable_reason=None,
        node_count=2,
        agent_roles=("builder",),
        orders=(),
        wait_answer_schemas=(),
        node_previews=(
            _agent_preview("implement"),
            _agent_preview("review", depends_on=("implement",)),
        ),
        loops=(),
        name="Two agents in a line",
        description=None,
    )

    assert resource.node_previews[1].depends_on == ("implement",)


def test_v3_graph_accepts_an_entry_preview_with_no_edges() -> None:
    resource = WorkflowGraphResourceV3(
        workflow_format_version=3,
        executable=True,
        not_executable_reason=None,
        node_count=1,
        agent_roles=("builder",),
        orders=(),
        wait_answer_schemas=(),
        node_previews=(_agent_preview("implement"),),
        loops=(),
        name="One agent",
        description=None,
    )

    assert resource.node_previews[0].depends_on == ()


def test_v3_graph_refuses_a_depends_on_that_names_no_preview() -> None:
    with pytest.raises(ValidationError, match="depends_on"):
        WorkflowGraphResourceV3(
            workflow_format_version=3,
            executable=True,
            not_executable_reason=None,
            node_count=1,
            agent_roles=("builder",),
            orders=(),
            wait_answer_schemas=(),
            node_previews=(_agent_preview("review", depends_on=("implement",)),),
            loops=(),
            name="Broken edge",
            description=None,
        )


def test_v3_graph_projection_carries_a_declared_loop_and_its_verdict() -> None:
    graph = parse_workflow_document(VERDICT_LOOP_DOCUMENT)

    resource = graph_resource(graph, None)

    assert isinstance(resource, WorkflowGraphResourceV3)
    assert len(resource.loops) == 1
    loop = resource.loops[0]
    assert loop.id == "until_reviewed"
    assert loop.member_node_ids == ("implement", "review")
    assert loop.maximum_rounds == VERDICT_LOOP_MAXIMUM_ROUNDS
    assert loop.repeat_while == WorkflowLoopVerdictResourceV3(
        node="review", verdict="revise"
    )


def test_v3_graph_projection_carries_no_loops_when_the_document_declares_none() -> None:
    graph = parse_workflow_document(V3_DOCUMENT)

    resource = graph_resource(graph, None)

    assert isinstance(resource, WorkflowGraphResourceV3)
    assert resource.loops == ()


def test_v3_graph_projection_names_a_wait_nodes_schema_hull_unresolved() -> None:
    """Reading the schema's own bytes to say `boolean` or `enum` needs a
    `PublishedRevisionResolver` read the API layer may not make itself by
    matching a port record (`api-port-record-problems`,
    `scripts/check_architecture.py`) -- `atelier2.application.read_workflow_revisions`
    does that read and hands this projection the plain verdict. A caller that
    supplies none (or one that names no entry for this node) gets the hull
    unresolved, exactly as `orders` already does, and classifies `free`."""
    document = b"""format_version: 3
name: Approve or send back
nodes:
  - id: approve
    type: wait
    prompt: Approve the candidate or send it back.
    outputs:
      - name: decision
        schema: {ref: decision, revision: schema-decision}
"""
    graph = parse_workflow_document(document)

    resource = graph_resource(graph, None)

    assert isinstance(resource, WorkflowGraphResourceV3)
    assert resource.wait_answer_schemas == (
        WaitAnswerSchemaResourceV3(
            node_id="approve",
            schema=WorkflowDeclaredSchemaResourceV3(
                ref="decision", revision="schema-decision"
            ),
            kind="free",
            values=None,
        ),
    )


def test_v3_graph_projection_applies_a_supplied_wait_answer_classification() -> None:
    """The projection never resolves a schema itself; it only ever matches an
    already-resolved verdict by node id, one wait node classifying and the
    other -- not named by any supplied verdict -- falling back to `free`."""
    document = b"""format_version: 3
name: Ship or hold, or say why
nodes:
  - id: ship
    type: wait
    prompt: Ship it?
    outputs:
      - name: decision
        schema: {ref: decision, revision: schema-decision}
  - id: reason
    type: wait
    prompt: Say why, freely.
    outputs:
      - name: note
        schema: {ref: note, revision: schema-note}
"""
    graph = parse_workflow_document(document)

    resource = graph_resource(
        graph,
        None,
        (WaitAnswerClassification(node_id="ship", kind="boolean"),),
    )

    assert isinstance(resource, WorkflowGraphResourceV3)
    by_node_id = {entry.node_id: entry for entry in resource.wait_answer_schemas}
    assert by_node_id["ship"].kind == "boolean"
    assert by_node_id["ship"].values is None
    assert by_node_id["reason"].kind == "free"


def test_v3_graph_projection_applies_a_supplied_enum_classification_with_its_values() -> (
    None
):
    document = b"""format_version: 3
name: Approve or revise
nodes:
  - id: verdict
    type: wait
    prompt: Approve or revise?
    outputs:
      - name: decision
        schema: {ref: decision, revision: schema-decision}
"""
    graph = parse_workflow_document(document)

    resource = graph_resource(
        graph,
        None,
        (
            WaitAnswerClassification(
                node_id="verdict", kind="enum", values=('"approve"', '"revise"')
            ),
        ),
    )

    assert isinstance(resource, WorkflowGraphResourceV3)
    entry = resource.wait_answer_schemas[0]
    assert entry.kind == "enum"
    assert entry.values == ('"approve"', '"revise"')


def test_wait_answer_schema_requires_values_exactly_for_an_enum_kind() -> None:
    with pytest.raises(ValidationError):
        WaitAnswerSchemaResourceV3(
            node_id="approve",
            schema=WorkflowDeclaredSchemaResourceV3(ref="decision", revision="hash"),
            kind="enum",
            values=None,
        )
    with pytest.raises(ValidationError):
        WaitAnswerSchemaResourceV3(
            node_id="approve",
            schema=WorkflowDeclaredSchemaResourceV3(ref="decision", revision="hash"),
            kind="boolean",
            values=("true",),
        )


def test_v3_graph_accepts_a_wait_preview_with_its_matching_answer_schema() -> None:
    resource = WorkflowGraphResourceV3(
        workflow_format_version=3,
        executable=False,
        not_executable_reason="waits for a runtime that binds waits",
        node_count=1,
        agent_roles=(),
        orders=(),
        wait_answer_schemas=(_wait_answer_schema("approve", kind="enum"),),
        node_previews=(_wait_preview("approve"),),
        loops=(),
        name="One wait",
        description=None,
    )

    assert resource.wait_answer_schemas[0].node_id == "approve"


def test_v3_graph_refuses_a_wait_answer_schema_naming_no_wait_preview() -> None:
    with pytest.raises(ValidationError, match="answer schema"):
        WorkflowGraphResourceV3(
            workflow_format_version=3,
            executable=True,
            not_executable_reason=None,
            node_count=1,
            agent_roles=("builder",),
            orders=(),
            wait_answer_schemas=(_wait_answer_schema("approve"),),
            node_previews=(_agent_preview("other"),),
            loops=(),
            name="Mismatched wait answer schema",
            description=None,
        )


def test_models_are_frozen_strict_and_forbid_extra_fields() -> None:
    with pytest.raises(ValidationError):
        AgentNodeResource.model_validate(
            {
                "type": "agent",
                "node_id": "agent",
                "job": "job",
                "output": "payload",
                "next_node_id": "action",
                "provider": "forbidden",
            }
        )
    with pytest.raises(ValidationError):
        RunResource.model_validate(
            {
                "run_id": "run",
                "public_run_reference": "run1.cnVu",
                "workflow_revision_hash": HASH,
                "state_version": True,
                "state": "STARTED",
                "current_node": agent_node(),
                "waiting": NoWaitingResource(type="NONE"),
                "terminal_hash": None,
                "latest_event_cursor": None,
            }
        )


def test_frozen_model_rejects_an_actual_assignment() -> None:
    resource = agent_node()

    with pytest.raises(ValidationError, match="frozen_instance"):
        resource.job = "mutated"

    assert resource.job == "job"


def test_event_union_forbids_fields_from_another_variant() -> None:
    with pytest.raises(ValidationError):
        AgentCompletedEventResource.model_validate(
            {
                "event": "AGENT_COMPLETED",
                "cursor": "event1.cnVu.1",
                "sequence": 1,
                "public_run_reference": "run1.cnVu",
                "workflow_revision_hash": HASH,
                "node_id": "agent",
                "node_execution_id": EXECUTION,
                "event_hash": HASH,
                "output": "payload",
                "payload_hash": HASH,
                "receipt": {},
            }
        )


@pytest.mark.parametrize("discriminator", [None, "UNKNOWN", 17])
def test_event_union_rejects_missing_unknown_and_non_string_discriminators(
    discriminator: object,
) -> None:
    candidate: dict[str, object] = {
        "event": discriminator,
        "cursor": "event1.cnVu.1",
        "sequence": 1,
        "public_run_reference": "run1.cnVu",
        "workflow_revision_hash": HASH,
        "node_id": "agent",
        "node_execution_id": EXECUTION,
        "event_hash": HASH,
        "output": "payload",
        "payload_hash": HASH,
    }
    if discriminator is None:
        del candidate["event"]

    with pytest.raises(ValidationError):
        TypeAdapter(RunEventResource).validate_python(candidate)
