from __future__ import annotations

from atelier2.contracts.agents import (
    AgentExecutionRequest,
    AgentExecutionResult,
    AgentExecutorBinding,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
)

EXACT_OUTPUT_EXECUTOR_BINDING = AgentExecutorBinding(
    AgentExecutorRevision("exact-output/v1"),
    AgentExecutorOperationalIdentity("format-version-1"),
)


class ExactOutputAgentExecutorFactory:
    @property
    def binding(self) -> AgentExecutorBinding:
        return EXACT_OUTPUT_EXECUTOR_BINDING

    def open(self) -> ExactOutputAgentExecutor:
        return ExactOutputAgentExecutor()


class ExactOutputAgentExecutor:
    def execute(self, request: AgentExecutionRequest) -> AgentExecutionResult:
        return AgentExecutionResult(request.exact_output.output_bytes)

    def close(self) -> None:
        return
