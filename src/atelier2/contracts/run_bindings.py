from __future__ import annotations

from dataclasses import dataclass

from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentBindingSetHash,
    ResolvedAgentBinding,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.runs import Run, RunId, RunState, WorkflowRevisionHash


@dataclass(frozen=True)
class RunV2:
    run_id: RunId
    revision_hash: WorkflowRevisionHash
    binding_set_hash: AgentBindingSetHash
    agent_bindings: tuple[ResolvedAgentBinding, ...]
    state: RunState
    current_node_id: str
    state_version: int
    last_event_sequence: int
    terminal_hash: Sha256Hash | None = None

    def __post_init__(self) -> None:
        Run.validate_head(
            self.current_node_id,
            self.state,
            self.state_version,
            self.last_event_sequence,
            self.terminal_hash,
        )
        ordered = tuple(
            sorted(
                self.agent_bindings,
                key=lambda binding: binding.role.value.encode("utf-8"),
            )
        )
        if len({binding.role for binding in ordered}) != len(ordered):
            raise ValueError("resolved run agent roles must be unique")
        expected = AgentBindingSet(
            tuple(
                AgentBinding(binding.role, binding.configuration.revision_hash)
                for binding in ordered
            )
        )
        if expected.binding_set_hash != self.binding_set_hash:
            raise ValueError("run agent binding set hash differs from its matrix")
        object.__setattr__(self, "agent_bindings", ordered)


type AnyRun = Run | RunV2
