from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from atelier2.api.models import (
    ActionNodeResource,
    AgentAttemptResourceV2,
    AgentCompletedEventResource,
    AgentNodeResource,
    NoWaitingResource,
    RunEventResource,
    RunResource,
    SubworkflowNodeResource,
    WaitingInputResource,
    WaitingReconciliationResource,
    WaitNodeResource,
)

HASH = "0" * 64
EXECUTION = "1" * 64


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
