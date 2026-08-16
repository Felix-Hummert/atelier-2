from __future__ import annotations

from dataclasses import dataclass

from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentBindingSetHash,
    ResolvedAgentBinding,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.run_configuration_v3 import RunConfigurationRevisionHash
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


@dataclass(frozen=True)
class RunV3:
    """A started V3 run: the same role matrix, under its own format's truth.

    It is not a `RunV2` with a different number on it. A V2 run's agent bindings
    are the whole of what it was started with; a V3 run is additionally bound to
    the run configuration its document resolved, and carrying that hash here is
    what stops a V3 run from being read back as a V2 one that happens to fit.
    Until a resolver exists the field is honestly absent rather than invented --
    but the type is separate from the day it exists, because widening `RunV2`
    later would mean every V2 reader had silently been reading V3 rows.
    """

    run_id: RunId
    revision_hash: WorkflowRevisionHash
    binding_set_hash: AgentBindingSetHash
    agent_bindings: tuple[ResolvedAgentBinding, ...]
    state: RunState
    current_node_id: str
    state_version: int
    last_event_sequence: int
    terminal_hash: Sha256Hash | None = None
    run_configuration_revision_hash: RunConfigurationRevisionHash | None = None

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


type AnyRun = Run | RunV2 | RunV3
type AnyBoundRun = RunV2 | RunV3
