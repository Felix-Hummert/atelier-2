"""A format-3 event keeps its own family, and the V1 pin stays pinned.

The mapper used to treat every non-2 event as V1. That dressed a V3 completion
as utf-8 `output` without format or rail, and sent a V3 failure down the V1
"cannot carry" path. These cases pin the V3 family at that owner.
"""

from __future__ import annotations

import importlib.util
from dataclasses import replace
from pathlib import Path

import pytest

from atelier2.api.projection import events as events_module
from atelier2.api.projection.events import run_event_resource
from atelier2.api.references import encode_canonical_base64
from atelier2.api.wire.events import AgentCompletedEventResource
from atelier2.contracts.executions import NodeExecutionId, RunEvent, RunEventKind
from atelier2.contracts.run_events import PersistedRunEvent
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.contracts.workflow_formats import WorkflowFormatVersion
from tests.api import test_agent_attempts as v2_events
from tests.api.test_agent_attempts import SERVED_RAIL
from tests.api.test_v1_event_vocabulary import v1_projection

RUN_ID = RunId("v3-event-vocabulary")
REVISION_HASH = WorkflowRevisionHash("0" * 64)
NODE_ID = "implement"
ATTEMPT_ID = "a" * 64
FAILURE_PAYLOAD = b"PROCESS_EXITED_UNSUCCESSFULLY"
COMPLETED_PAYLOAD = b"hello"


def v3_projection(kind: RunEventKind, payload: bytes) -> PersistedRunEvent:
    event = RunEvent(
        RUN_ID,
        REVISION_HASH,
        1,
        NODE_ID,
        NodeExecutionId.for_node(RUN_ID, REVISION_HASH, NODE_ID),
        kind,
        payload,
        agent_attempt_id=ATTEMPT_ID,
        attempt_ordinal=1,
    )
    return PersistedRunEvent(event, None, WorkflowFormatVersion.V3)


@pytest.mark.proves("a-format-three-event-answers-in-the-shape-that-says-so")
def test_format_3_agent_completed_is_a_v3_resource_not_the_frozen_v1() -> None:
    projection = v3_projection(RunEventKind.AGENT_COMPLETED, COMPLETED_PAYLOAD)

    resource = run_event_resource(projection, SERVED_RAIL)

    dumped = resource.model_dump(mode="json")
    assert dumped["workflow_format_version"] == 3
    assert dumped["output_base64"] == encode_canonical_base64(COMPLETED_PAYLOAD)
    assert dumped["output_hash"] == projection.event.payload_hash.value
    assert dumped["node_rail"] == [
        entry.model_dump(mode="json") for entry in SERVED_RAIL
    ]
    assert dumped["attempt_id"] == ATTEMPT_ID
    assert dumped["attempt_ordinal"] == 1
    assert "output" not in dumped
    assert not isinstance(resource, AgentCompletedEventResource)
    assert type(resource).__name__ == "AgentCompletedEventResourceV3"


@pytest.mark.proves("a-format-three-event-answers-in-the-shape-that-says-so")
def test_format_3_agent_failed_is_a_v3_failure_not_the_v1_cannot_carry_path() -> None:
    resource = run_event_resource(
        v3_projection(RunEventKind.AGENT_FAILED, FAILURE_PAYLOAD), SERVED_RAIL
    )

    dumped = resource.model_dump(mode="json")
    assert dumped["workflow_format_version"] == 3
    assert dumped["event"] == "AGENT_FAILED"
    assert dumped["failure_code"] == "PROCESS_EXITED_UNSUCCESSFULLY"
    assert dumped["reason"] is None
    assert dumped["attempt_id"] == ATTEMPT_ID
    assert dumped["node_rail"] == [
        entry.model_dump(mode="json") for entry in SERVED_RAIL
    ]
    assert type(resource).__name__ == "AgentFailedEventResourceV3"


@pytest.mark.proves("an-agent-failed-event-carries-the-stored-receipt-reason")
def test_a_v3_failure_carries_the_stored_receipt_reason_when_one_was_written() -> None:
    words = "output-schema-refused: instance-not-json: Expecting value"
    projection = replace(
        v3_projection(RunEventKind.AGENT_FAILED, b"OUTPUT_SCHEMA_REFUSED"),
        node_receipt_reason=words,
    )

    dumped = run_event_resource(projection, SERVED_RAIL).model_dump(mode="json")

    assert dumped["failure_code"] == "OUTPUT_SCHEMA_REFUSED"
    assert dumped["reason"] == words


@pytest.mark.proves("a-format-three-event-answers-in-the-shape-that-says-so")
def test_a_v3_failure_admits_project_verification_failed() -> None:
    words = "project-verification-failed: exit 1"
    projection = replace(
        v3_projection(RunEventKind.AGENT_FAILED, b"PROJECT_VERIFICATION_FAILED"),
        node_receipt_reason=words,
    )

    dumped = run_event_resource(projection, SERVED_RAIL).model_dump(mode="json")

    assert dumped["failure_code"] == "PROJECT_VERIFICATION_FAILED"
    assert dumped["reason"] == words


@pytest.mark.proves("a-v3-line-stops-for-a-person-and-their-answer-carries-it-on")
def test_format_3_waiting_input_says_a_person_is_owed_a_move_and_names_no_type() -> (
    None
):
    """The pause reads back in the V3 family, without the V2 answer vocabulary.

    A V1 or V2 Wait node declares `answer_type: integer` and its event says so. A
    V3 Wait node declares an output with a schema instead, so naming a type here
    would state a shape nothing in this format enforces.
    """
    projection = v3_projection(RunEventKind.WAITING_INPUT, b"")

    resource = run_event_resource(projection, SERVED_RAIL)

    dumped = resource.model_dump(mode="json")
    assert dumped["workflow_format_version"] == 3
    assert dumped["event"] == "WAITING_INPUT"
    assert "answer_type" not in dumped
    assert dumped["node_rail"] == [
        entry.model_dump(mode="json") for entry in SERVED_RAIL
    ]


@pytest.mark.proves("a-v3-line-stops-for-a-person-and-their-answer-carries-it-on")
def test_format_3_wait_answered_carries_the_exact_bytes_a_person_sent() -> None:
    """A V3 answer is whatever its schema admits, so it travels as bytes.

    The V2 shape renders a decimal string, which only its own `integer` wait can
    honestly produce; a JSON string, object or array read through that shape
    would be a value the wire misdescribes.
    """
    answer = b'{"verdict": "approved"}'
    projection = v3_projection(RunEventKind.WAIT_ANSWERED, answer)

    resource = run_event_resource(projection, SERVED_RAIL)

    dumped = resource.model_dump(mode="json")
    assert dumped["workflow_format_version"] == 3
    assert dumped["event"] == "WAIT_ANSWERED"
    assert dumped["answer_base64"] == encode_canonical_base64(answer)
    assert dumped["answer_hash"] == projection.event.payload_hash.value
    assert "answer" not in dumped


@pytest.mark.proves("a-format-three-event-answers-in-the-shape-that-says-so")
def test_a_v3_run_cannot_answer_with_a_kind_its_nodes_never_write() -> None:
    """A V3 node is an Agent or a Wait, so a subworkflow completion is a lie."""
    subworkflow = PersistedRunEvent(
        event=RunEvent(
            run_id=RUN_ID,
            revision_hash=REVISION_HASH,
            event_sequence=1,
            node_id="done",
            node_execution_id=NodeExecutionId.for_node(RUN_ID, REVISION_HASH, "done"),
            event_kind=RunEventKind.SUBWORKFLOW_COMPLETED,
            payload=b"5",
        ),
        workflow_format_version=WorkflowFormatVersion.V3,
        receipt=None,
    )

    with pytest.raises(ValueError, match="a V3 run cannot carry SUBWORKFLOW_COMPLETED"):
        run_event_resource(subworkflow, SERVED_RAIL)


def test_format_1_agent_failed_still_raises_the_v1_cannot_carry_path() -> None:
    with pytest.raises(ValueError, match="a V1 run cannot carry AGENT_FAILED"):
        run_event_resource(v1_projection(RunEventKind.AGENT_FAILED), ())


def test_format_2_mapping_is_unchanged() -> None:
    v2_events.test_v2_attempt_and_failed_event_have_exact_wire_shape()


def test_restoring_the_v2_else_v1_dispatch_reds_the_v3_agent_cases(
    tmp_path: Path,
) -> None:
    """The old `if format == 2 else V1` branch would dress 1 and raise 2."""

    source = Path(events_module.__file__).read_text(encoding="utf-8")
    restored = source.replace(
        "    if projection.workflow_format_version is WorkflowFormatVersion.V3:\n"
        "        return _run_event_resource_v3(projection, node_rail)\n",
        "",
    )
    assert restored != source, "the V3 dispatch the mutation removes is missing"
    mutated = tmp_path / "events_restored.py"
    mutated.write_text(restored, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(
        "atelier2.api.projection.events_restored", mutated
    )
    assert spec is not None and spec.loader is not None
    restored_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(restored_module)

    completed = restored_module.run_event_resource(
        v3_projection(RunEventKind.AGENT_COMPLETED, COMPLETED_PAYLOAD), SERVED_RAIL
    )
    assert isinstance(completed, AgentCompletedEventResource)
    assert "workflow_format_version" not in completed.model_dump()

    with pytest.raises(ValueError, match="a V1 run cannot carry AGENT_FAILED"):
        restored_module.run_event_resource(
            v3_projection(RunEventKind.AGENT_FAILED, FAILURE_PAYLOAD), SERVED_RAIL
        )
