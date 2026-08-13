from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from atelier2.contracts.agent_attempts import AgentAttempt
from atelier2.contracts.agents import AgentExecutionRequestV2, AgentExecutionResult


@dataclass(frozen=True)
class AgentAttemptClaimedByThisCall:
    attempt: AgentAttempt


@dataclass(frozen=True)
class AgentAttemptSucceeded:
    attempt: AgentAttempt
    successor_node_id: str


@dataclass(frozen=True)
class AgentAttemptFailed:
    attempt: AgentAttempt


@dataclass(frozen=True)
class AgentAttemptPossiblyRan:
    attempt: AgentAttempt


type AgentAttemptClaimResult = (
    AgentAttemptClaimedByThisCall
    | AgentAttemptSucceeded
    | AgentAttemptFailed
    | AgentAttemptPossiblyRan
)
type AgentAttemptExecutionOutcome = (
    AgentAttemptSucceeded | AgentAttemptFailed | AgentAttemptPossiblyRan
)


class AgentAttemptStore(Protocol):
    def prepare(self, request: AgentExecutionRequestV2) -> AgentAttempt: ...

    def claim(self, request: AgentExecutionRequestV2) -> AgentAttemptClaimResult: ...

    def complete_success(
        self, request: AgentExecutionRequestV2, result: AgentExecutionResult
    ) -> AgentAttemptSucceeded: ...

    def complete_known_failure(
        self, request: AgentExecutionRequestV2
    ) -> AgentAttemptFailed: ...
