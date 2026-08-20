from __future__ import annotations

from collections.abc import Iterator
from typing import cast

from atelier2.application.converge_driverless_attempts import (
    converge_driverless_attempts,
)
from atelier2.contracts.agent_attempts import AgentAttempt, CancelAgentAttemptRequest
from atelier2.contracts.pages import PageLimit
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationStale,
    AgentAttemptStore,
)
from atelier2.ports.agent_executions import (
    AgentAttemptWorkspaceOwner,
    AgentProcessRunner,
)
from tests.scenarios.agents import (
    agent_attempt_execution,
    agent_execution_request_v2,
    prepared_agent_attempt,
)


class StreamingDriverlessAttempts:
    def __init__(self, attempts: tuple[AgentAttempt, ...]) -> None:
        self._attempts = attempts
        self.processed: list[AgentAttempt] = []

    def iter_driverless_attempts(self, page_limit: PageLimit) -> Iterator[AgentAttempt]:
        del page_limit
        for index, attempt in enumerate(self._attempts):
            assert len(self.processed) == index
            yield attempt

    def request_cancellation(
        self, request: CancelAgentAttemptRequest
    ) -> AgentAttemptCancellationStale:
        attempt = self._attempts[len(self.processed)]
        assert request.attempt_id == attempt.attempt_id
        self.processed.append(attempt)
        return AgentAttemptCancellationStale()


def test_driverless_convergence_consumes_and_discards_each_attempt_in_turn() -> None:
    attempts = tuple(
        prepared_agent_attempt(
            agent_attempt_execution(
                agent_execution_request_v2(f"driverless/stream/{index}")
            )
        )
        for index in range(3)
    )
    store = StreamingDriverlessAttempts(attempts)

    result = converge_driverless_attempts(
        cast(AgentAttemptStore, store),
        cast(AgentProcessRunner, object()),
        cast(AgentAttemptWorkspaceOwner, object()),
    )

    assert result is None
    assert store.processed == list(attempts)
