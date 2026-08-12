from __future__ import annotations

from typing import Protocol

from atelier2.contracts.agents import (
    AgentExecutionRequest,
    AgentExecutionResult,
    AgentExecutorBinding,
)


class AgentExecutor(Protocol):
    def execute(self, request: AgentExecutionRequest) -> AgentExecutionResult: ...

    def close(self) -> None: ...


class AgentExecutorFactory(Protocol):
    @property
    def binding(self) -> AgentExecutorBinding: ...

    def open(self) -> AgentExecutor: ...
