from __future__ import annotations

from typing import Protocol

from atelier2.contracts.agent_attempts import (
    CancelAgentAttemptRequest,
    RunnerGenerationBinding,
    RunnerInvocationId,
    RunnerTerminalEvidenceHash,
)


class RunnerSessionCore(Protocol):
    """The narrow Core mutations a runner-session transport is allowed to ask for."""

    def arm(
        self, binding: RunnerGenerationBinding, invocation: RunnerInvocationId
    ) -> None: ...

    def commit_terminal_record(
        self, binding: RunnerGenerationBinding, record: bytes
    ) -> RunnerTerminalEvidenceHash: ...

    def acknowledge(
        self,
        binding: RunnerGenerationBinding,
        evidence_hash: RunnerTerminalEvidenceHash,
        tombstone: bytes,
    ) -> None: ...

    def cancel(self) -> CancelAgentAttemptRequest: ...
