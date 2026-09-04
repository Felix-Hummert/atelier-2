"""An attempt records decoded transcript events at the application boundary."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

import pytest

from atelier2.application.execute_agent_attempt import execute_agent_attempt
from atelier2.contracts.agent_attempts import (
    AgentAttempt,
    AgentAttemptId,
    AgentAttemptState,
)
from atelier2.contracts.agent_transcripts import (
    AssistantTurn,
    AttemptTranscript,
    TranscriptBeforeMoments,
    TranscriptMomentOrigin,
    TranscriptRecordedMoment,
)
from atelier2.contracts.agents import AgentExecutionResult
from atelier2.contracts.executions import AgentAttemptExecution
from atelier2.contracts.when import RecordedAt
from atelier2.contracts.workflows import RunCompletes
from atelier2.ports.agent_attempts import (
    AgentAttemptClaimedByThisCall,
    AgentAttemptStore,
    AgentAttemptSucceeded,
)
from atelier2.ports.agent_executions import (
    AgentAttemptWorkspaceLease,
    AgentProcessCommand,
    AgentProcessCompletion,
    AgentProcessInvocation,
)
from tests.scenarios.agents import (
    FakeAgentSession,
    agent_attempt_execution,
    agent_execution_request_v2,
    leased_directory_identity,
    prepared_agent_attempt,
)


@dataclass
class _ClaimingStore:
    attempt: AgentAttempt | None = None
    completed_result: AgentExecutionResult | None = None

    def prepare(self, execution: AgentAttemptExecution) -> AgentAttempt:
        self.attempt = prepared_agent_attempt(execution)
        return self.attempt

    def claim(self, execution: AgentAttemptExecution) -> AgentAttemptClaimedByThisCall:
        del execution
        assert self.attempt is not None
        self.attempt = replace(
            self.attempt,
            state=AgentAttemptState.LAUNCH_ARMED,
            state_version=self.attempt.state_version + 1,
        )
        return AgentAttemptClaimedByThisCall(self.attempt)

    def complete_success(
        self,
        execution: AgentAttemptExecution,
        result: AgentExecutionResult,
        redemption: object,
    ) -> AgentAttemptSucceeded:
        del execution, redemption
        self.completed_result = result
        assert self.attempt is not None
        return AgentAttemptSucceeded(self.attempt, RunCompletes())


class _TranscriptExecutor:
    def prepare_process(self, request: object) -> AgentProcessCommand:
        del request
        return AgentProcessCommand(("/bin/true",), standard_output_frame_bytes=1)

    def decode_process_completion(
        self,
        invocation: AgentProcessInvocation,
        completion: AgentProcessCompletion,
    ) -> AgentExecutionResult:
        del invocation, completion
        return AgentExecutionResult(
            b'"done"', AttemptTranscript.of((AssistantTurn("The work is done."),))
        )

    def release_credential_channel(self, command: AgentProcessCommand) -> None:
        del command

    def close(self) -> None:
        return None


@dataclass
class _Workspaces:
    directory: Path

    def preflight(self) -> None:
        return None

    def acquire(self, attempt_id: AgentAttemptId) -> AgentAttemptWorkspaceLease:
        return leased_directory_identity(attempt_id, self.directory)

    def release(self, attempt_id: AgentAttemptId) -> None:
        del attempt_id


@pytest.mark.proves("every-transcript-event-carries-its-moment")
def test_proves_every_transcript_event_carries_its_moment_when_recorded(
    tmp_path: Path,
) -> None:
    """proves(every-transcript-event-carries-its-moment)"""

    store = _ClaimingStore()
    recording_moment = RecordedAt("2026-08-29T12:00:00Z")
    execute_agent_attempt(
        agent_attempt_execution(agent_execution_request_v2()),
        _TranscriptExecutor(),
        cast(AgentAttemptStore, store),
        FakeAgentSession(AgentProcessCompletion(0, b'"done"', b"")),
        _Workspaces(tmp_path / "workspace"),
        clock=lambda: recording_moment,
    )

    assert store.completed_result is not None
    recorded = store.completed_result.transcript
    assert recorded is not None
    assert recorded.document.startswith(b'{"kind":"attempt-transcript/v2",')
    assert all(
        event.moment
        == TranscriptRecordedMoment(recording_moment, TranscriptMomentOrigin.RECORDED)
        for event in recorded.events
    )


@pytest.mark.proves("every-transcript-event-carries-its-moment")
def test_proves_every_v1_transcript_event_explicitly_predates_moments() -> None:
    """proves(every-transcript-event-carries-its-moment)"""

    v1_document = (
        b'{"kind":"attempt-transcript/v1","events":['
        b'{"event":"assistant-turn","text":"Earlier work.","redacted":false}]}'
    )

    historical = AttemptTranscript.from_document(v1_document)

    assert historical.document == v1_document
    assert isinstance(historical.events[0].moment, TranscriptBeforeMoments)
    with pytest.raises(ValueError, match="cannot be given a recording moment"):
        historical.with_recorded_moment(RecordedAt("2026-08-29T12:00:00Z"))
