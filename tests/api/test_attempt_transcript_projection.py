"""Node detail projects a stored attempt transcript as decoded events."""

from __future__ import annotations

from typing import Any

from atelier2.api.projection.runs import node_detail_resource
from atelier2.contracts.agent_transcripts import (
    AssistantTurn,
    AttemptTranscript,
    ToolCalled,
    ToolReturned,
    UnrecognisedProviderOutput,
    Usage,
)
from atelier2.contracts.run_projections import NodeDetail, NodeState
from atelier2.contracts.runs import RunId
from atelier2.contracts.secret_redaction import REDACTION_MARKER


def planted_credential(issuer: str, body: str) -> str:
    """A credential-shaped canary, assembled here instead of spelled out."""

    return f"{issuer}{body}"


def _detail(
    transcript: AttemptTranscript | None = None,
    *,
    state: NodeState = NodeState.SUCCEEDED,
) -> NodeDetail:
    return NodeDetail(
        run_id=RunId("run-transcript"),
        node_id="implement",
        state=state,
        job=None,
        job_hash=None,
        answer=None,
        provenance=None,
        refusal=None,
        transcript=transcript,
    )


def _wire(detail: NodeDetail) -> dict[str, Any]:
    return node_detail_resource(detail).model_dump(mode="json")


def test_tool_called_returned_assistant_turn_and_usage_project_as_events() -> None:
    transcript = AttemptTranscript.of(
        [
            ToolCalled("Read", '{"file_path":"/etc/hosts"}'),
            ToolReturned("Read", "127.0.0.1 localhost"),
            AssistantTurn("The host file names localhost."),
            Usage(1_200, 48),
        ]
    )

    dumped = _wire(_detail(transcript))

    assert dumped["transcript"] == {
        "events": [
            {
                "event": "tool-called",
                "name": "Read",
                "arguments": '{"file_path":"/etc/hosts"}',
                "redacted": False,
            },
            {
                "event": "tool-returned",
                "name": "Read",
                "result": "127.0.0.1 localhost",
                "redacted": False,
            },
            {
                "event": "assistant-turn",
                "text": "The host file names localhost.",
                "redacted": False,
            },
            {
                "event": "usage",
                "input_tokens": 1_200,
                "output_tokens": 48,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        ]
    }
    assert "kind" not in dumped["transcript"]
    assert "document" not in dumped["transcript"]


def test_a_failed_attempt_whose_only_step_is_unrecognised_provider_output() -> None:
    canary = planted_credential("sk-ant", "-plantedcanarysecret0123456789")
    transcript = AttemptTranscript.of(
        [UnrecognisedProviderOutput(f"fatal: token {canary} rejected")]
    )

    dumped = _wire(_detail(transcript, state=NodeState.FAILED))

    assert dumped["transcript"] == {
        "events": [
            {
                "event": "unrecognised-provider-output",
                "text": f"fatal: token {REDACTION_MARKER} rejected",
                "redacted": True,
            }
        ]
    }
    assert canary not in str(dumped)


def test_a_canary_in_a_tool_result_or_stdout_never_appears_on_the_wire() -> None:
    canary = planted_credential("sk-ant", "-plantedcanarysecret0123456789")
    transcript = AttemptTranscript.of(
        [
            ToolReturned("Bash", f"ANTHROPIC_API_KEY={canary}\n"),
            UnrecognisedProviderOutput(f"fatal: {canary} was rejected"),
        ]
    )

    resource = node_detail_resource(_detail(transcript))
    dumped = resource.model_dump(mode="json")
    rendered = resource.model_dump_json()

    assert canary not in rendered
    assert REDACTION_MARKER in rendered
    assert all(event["redacted"] for event in dumped["transcript"]["events"])


def test_an_absent_transcript_omits_the_key() -> None:
    dumped = _wire(_detail())

    assert "transcript" not in dumped
