"""What the standard core does when a real agent's own bytes are played at it.

The seven captures in `tests/fixtures/grok_acp` are recordings, not a state
machine's exercise: they pin what this conversation asks for and concludes when
the frames are exactly the ones a released `grok agent stdio` wrote. What they
prove is therefore narrow and worth having -- the lifecycle survives a real
stream, the file requests are the recorded ones, and every permission this
vendor asks lands closed because the standard vocabulary cannot scope it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from atelier2.adapters.agent_client_protocol import (
    AcpMethod,
    AgentClientProtocolConversation,
)
from atelier2.adapters.newline_json_rpc import JsonObject
from atelier2.contracts.agent_attempts import AgentAttemptId
from atelier2.contracts.agent_permissions import PermissionRequest
from atelier2.contracts.agent_transcripts import (
    AssistantTurn,
    ToolCalled,
    ToolReturned,
    UnrecognisedProviderOutput,
)
from atelier2.ports.provider_conversations import (
    ProviderConversationBounds,
    ProviderConversationEnding,
    ProviderFilesystemAnswer,
    ProviderFilesystemEffect,
    ProviderFilesystemReply,
    ProviderFilesystemRequest,
    ProviderSessionEvent,
    ProviderStandardInput,
    ProviderTerminalOutcome,
    ProviderTerminalReason,
)

CAPTURES = Path(__file__).parents[1] / "fixtures" / "grok_acp"
ATTEMPT = AgentAttemptId("b" * 64)
FROM_THE_AGENT = "<-"
ROOM_FOR_A_RECORDED_STREAM = ProviderConversationBounds(
    1_048_576, 262_144, 65_536, 4_096, 131_072
)
TOOL_CALLS_NO_CAPTURE_REACHES = 64
UNREAD_STEP = UnrecognisedProviderOutput
RECORDED_STEPS = (AssistantTurn, ToolCalled, ToolReturned, UNREAD_STEP)


@dataclass(frozen=True)
class _Recorded:
    """What one capture's own frames say the conversation should have seen."""

    frames: tuple[JsonObject, ...]

    def spoken_by_the_agent(self) -> Iterator[JsonObject]:
        return iter(self.frames)

    def permissions_asked(self) -> int:
        return sum(
            frame.get("method") == AcpMethod.REQUEST_PERMISSION for frame in self.frames
        )

    def files_asked(self) -> tuple[tuple[str, str, str], ...]:
        return tuple(
            (str(frame["method"]), _named(frame, "path"), _named(frame, "content"))
            for frame in self.frames
            if frame.get("method")
            in (AcpMethod.READ_TEXT_FILE, AcpMethod.WRITE_TEXT_FILE)
        )


def _named(frame: JsonObject, field_name: str) -> str:
    params = frame.get("params")
    if not isinstance(params, dict):
        return ""
    named = params.get(field_name)
    return named if isinstance(named, str) else ""


@dataclass
class _Replay:
    """One capture played into one conversation, with its answers given back."""

    conversation: AgentClientProtocolConversation
    files: list[ProviderFilesystemRequest] = field(default_factory=list)
    questions: list[PermissionRequest] = field(default_factory=list)
    written: list[JsonObject] = field(default_factory=list)
    steps: list[object] = field(default_factory=list)

    def play(self, recorded: _Recorded) -> None:
        self._act(self.conversation.open())
        for frame in recorded.spoken_by_the_agent():
            self._act(
                self.conversation.receive_output(
                    json.dumps(frame).encode("utf-8") + b"\n"
                )
            )

    def _act(self, actions: tuple[object, ...]) -> None:
        for action in actions:
            match action:
                case ProviderStandardInput(data):
                    self.written.append(json.loads(data))
                case ProviderFilesystemRequest() as asked:
                    self.files.append(asked)
                    self._answer(asked)
                case PermissionRequest() as question:
                    self.questions.append(question)
                case ProviderSessionEvent(step):
                    self.steps.append(step)
                case _:
                    continue

    def _answer(self, asked: ProviderFilesystemRequest) -> None:
        self.written.append(
            json.loads(
                self.conversation.answer_filesystem(
                    ProviderFilesystemReply(
                        asked.request_id, ProviderFilesystemAnswer.ANSWERED
                    )
                ).data
            )
        )


def _recorded(capture: str, run: int = 0) -> _Recorded:
    lines = (CAPTURES / capture).read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    return _Recorded(
        tuple(
            record["msg"]
            for record in records
            if record["dir"] == FROM_THE_AGENT and record.get("run", 0) == run
        )
    )


def _replayed(recorded: _Recorded) -> _Replay:
    replay = _Replay(
        AgentClientProtocolConversation(
            ATTEMPT,
            "append the line 'Probe line.' to README.md",
            Path("/attempts/probe"),
            ROOM_FOR_A_RECORDED_STREAM,
            TOOL_CALLS_NO_CAPTURE_REACHES,
        )
    )
    replay.play(recorded)
    return replay


REFUSED_BY_POLICY = ProviderTerminalOutcome(ProviderTerminalReason.POLICY_REFUSED)
CAPTURED_RUNS = (
    ("01-run-terminal-permission.jsonl", 0, REFUSED_BY_POLICY),
    ("01-run-terminal-permission.jsonl", 1, REFUSED_BY_POLICY),
    ("02-reject-once.jsonl", 0, REFUSED_BY_POLICY),
    ("03-write-text-file-error.jsonl", 0, REFUSED_BY_POLICY),
    (
        "04-session-cancel-mid-turn.jsonl",
        0,
        ProviderTerminalOutcome(
            ProviderTerminalReason.CANCELLED_BY_PROVIDER, "cancelled"
        ),
    ),
    ("05-max-turns-1.jsonl", 0, REFUSED_BY_POLICY),
    ("06-deny-write-path.jsonl", 0, REFUSED_BY_POLICY),
    (
        "07-no-leader-storage-footprint.jsonl",
        0,
        ProviderTerminalOutcome(ProviderTerminalReason.ENDED),
    ),
)


@pytest.mark.parametrize(("capture", "run", "outcome"), CAPTURED_RUNS)
def test_a_recorded_stream_ends_where_the_recording_ended(
    capture: str, run: int, outcome: ProviderTerminalOutcome
) -> None:
    """Two readings meet here: the agent's own `stopReason`, and this client's
    own refusal, which outranks it wherever a permission was asked at all."""
    replayed = _replayed(_recorded(capture, run))

    closing = replayed.conversation.finish(ProviderConversationEnding.OUTPUT_ENDED)

    assert closing.outcome == outcome


@pytest.mark.parametrize(("capture", "run", "outcome"), CAPTURED_RUNS)
def test_every_file_the_recording_asked_for_is_the_file_this_client_asked_about(
    capture: str, run: int, outcome: ProviderTerminalOutcome
) -> None:
    recorded = _recorded(capture, run)

    replayed = _replayed(recorded)

    assert (
        tuple(
            (
                AcpMethod.READ_TEXT_FILE
                if asked.effect is ProviderFilesystemEffect.READ
                else AcpMethod.WRITE_TEXT_FILE,
                str(asked.path),
                asked.content.decode("utf-8"),
            )
            for asked in replayed.files
        )
        == recorded.files_asked()
    )


@pytest.mark.parametrize(("capture", "run", "outcome"), CAPTURED_RUNS)
def test_no_permission_this_vendor_asks_can_be_scoped_by_the_standard_vocabulary(
    capture: str, run: int, outcome: ProviderTerminalOutcome
) -> None:
    """Every recorded request names its tool in `_meta` and its arguments in
    `rawInput`, and neither is standard: the effect is refused closed until a
    vendor vocabulary reads them."""
    recorded = _recorded(capture, run)

    replayed = _replayed(recorded)

    assert replayed.questions == []
    assert (
        sum("outcome" in _result_of(written) for written in replayed.written)
        == recorded.permissions_asked()
    )


@pytest.mark.parametrize(("capture", "run", "outcome"), CAPTURED_RUNS)
def test_a_vendor_extension_reaches_neither_a_read_step_nor_an_answer(
    capture: str, run: int, outcome: ProviderTerminalOutcome
) -> None:
    """A vendor field is evidence or it is nothing: what this core read stands
    in a step of its own vocabulary, and what it could not read stands as the
    message it arrived in, kept for a reader rather than acted on."""
    replayed = _replayed(_recorded(capture, run))

    read = [step for step in replayed.steps if not isinstance(step, UNREAD_STEP)]
    assert all(isinstance(step, RECORDED_STEPS) for step in replayed.steps)
    assert not [step for step in read if "x.ai" in repr(step)]
    assert [
        written["method"] for written in replayed.written if "method" in written
    ] == [
        AcpMethod.INITIALIZE,
        AcpMethod.SESSION_NEW,
        AcpMethod.SESSION_PROMPT,
    ]


def _result_of(written: JsonObject) -> JsonObject:
    result = written.get("result")
    return result if isinstance(result, dict) else {}
