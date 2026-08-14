from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from atelier2.contracts.agent_attempts import (
    AgentAttempt,
    AgentAttemptCancellationDisposition,
    AgentAttemptFailureCode,
    AgentProcessOwnerId,
    WatchdogGenerationId,
)
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_OUTPUT_BYTES_V2,
    AgentExecutionRequest,
    AgentExecutionRequestV2,
    AgentExecutionResult,
    AgentExecutorBinding,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    ProviderId,
)
from atelier2.contracts.executions import AgentAttemptExecution

MAXIMUM_AGENT_PROCESS_INPUT_BYTES = 49_152
MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES = 49_152

# The largest raw provider frame one process may return through this seam.
# It is deliberately distinct from the durable output bound: the frame carries
# the durable result *inside* a JSON envelope, so a bound equal to
# MAXIMUM_AGENT_OUTPUT_BYTES_V2 would refuse a result the durable contract
# accepts. Derivation, so no acceptable result can ever be refused as a frame:
#   * JSON string escaping expands one source byte to at most six frame bytes
#     (a C0 control byte becomes a six-character backslash-u escape), so a
#     durable-legal result occupies at most 6 * 49,152 = 294,912 frame bytes.
#   * Envelope metadata around it was measured at 1,313 bytes for one real
#     `claude -p --output-format json` answer (claude 2.1.221, one model in
#     modelUsage, no permission denials); 8,192 bytes allows roughly six times
#     that for more models, denials and future fields.
MAXIMUM_PROVIDER_FRAME_BYTES = (6 * MAXIMUM_AGENT_OUTPUT_BYTES_V2) + 8_192


class AgentExecutionMode(StrEnum):
    """How one executor drives its provider, as issue #9's ruling names it."""

    HEADLESS = "headless"
    INTERACTIVE = "interactive"


class AgentExecutor(Protocol):
    def execute(self, request: AgentExecutionRequest) -> AgentExecutionResult: ...

    def close(self) -> None: ...


class AgentExecutorFactory(Protocol):
    @property
    def binding(self) -> AgentExecutorBinding: ...

    def open(self) -> AgentExecutor: ...


@dataclass(frozen=True)
class AgentExecutorKey:
    provider_id: ProviderId
    executor_revision: AgentExecutorRevision


@dataclass(frozen=True)
class AgentExecutorManifestEntry:
    key: AgentExecutorKey
    operational_identity: AgentExecutorOperationalIdentity
    supported_modes: frozenset[AgentExecutionMode]


@dataclass(frozen=True)
class AgentExecutionFailure:
    code: AgentAttemptFailureCode


@dataclass(frozen=True)
class AgentProcessInvocation:
    """One provider process invocation, kept exclusively in live memory."""

    arguments: tuple[str, ...]
    working_directory: Path
    environment: tuple[tuple[str, str], ...] = ()
    standard_input: bytes = b""

    def __post_init__(self) -> None:
        # Only the program is required to be nonempty: an empty argument is how
        # a provider CLI is told "no tools" or "no settings sources" at all.
        if not self.arguments or not self.arguments[0]:
            raise ValueError("agent process program must be nonempty")
        if not self.working_directory.is_absolute():
            raise ValueError("agent process working directory must be absolute")
        names = tuple(name for name, _value in self.environment)
        if len(set(names)) != len(names) or any(not name for name in names):
            raise ValueError(
                "agent process environment names must be unique and nonempty"
            )
        if len(self.standard_input) > MAXIMUM_AGENT_PROCESS_INPUT_BYTES:
            raise ValueError(
                "agent process standard input exceeds "
                f"{MAXIMUM_AGENT_PROCESS_INPUT_BYTES} bytes"
            )


@dataclass(frozen=True)
class AgentProcessCompletion:
    return_code: int
    standard_output: bytes
    standard_error: bytes

    def __post_init__(self) -> None:
        if type(self.return_code) is not int:
            raise TypeError("agent process return code must be an integer")


class AgentExecutorV2(Protocol):
    def prepare_process(
        self, request: AgentExecutionRequestV2
    ) -> AgentProcessInvocation:
        """Prepare a live-only invocation without starting a child."""
        ...

    def decode_process_completion(
        self, completion: AgentProcessCompletion
    ) -> AgentExecutionResult | AgentExecutionFailure: ...

    def close(self) -> None: ...


class AgentProcessRunner(Protocol):
    def prepare(self, execution: AgentAttemptExecution) -> AgentAttempt: ...

    def launch_and_wait(
        self, execution: AgentAttemptExecution, invocation: AgentProcessInvocation
    ) -> AgentProcessCompletion: ...

    def cancel(
        self, attempt: AgentAttempt
    ) -> tuple[
        AgentAttemptCancellationDisposition,
        AgentProcessOwnerId,
        WatchdogGenerationId,
    ]: ...

    def recover(
        self, attempt: AgentAttempt
    ) -> tuple[
        AgentAttemptCancellationDisposition,
        AgentProcessOwnerId,
        WatchdogGenerationId,
    ]: ...

    def release(self, attempt: AgentAttempt) -> None: ...

    def finalize(self, execution: AgentAttemptExecution) -> None: ...


class AgentProcessOwnerNotLocal(Exception):
    pass


class AgentExecutorFactoryV2(Protocol):
    @property
    def key(self) -> AgentExecutorKey: ...

    @property
    def operational_identity(self) -> AgentExecutorOperationalIdentity: ...

    @property
    def supported_modes(self) -> frozenset[AgentExecutionMode]:
        """Every execution mode this executor can honestly drive."""
        ...

    def open(self) -> AgentExecutorV2: ...


@dataclass(frozen=True)
class AgentExecutorRegistryEntry:
    object_identity: int
    manifest_entry: AgentExecutorManifestEntry
    factory: AgentExecutorFactoryV2

    @property
    def key(self) -> AgentExecutorKey:
        return self.manifest_entry.key


class AgentExecutorRegistry:
    """Immutable host registry for exact provider/executor factories."""

    def __init__(self, factories: tuple[AgentExecutorFactoryV2, ...] = ()) -> None:
        object_identities = tuple(id(factory) for factory in factories)
        if len(set(object_identities)) != len(object_identities):
            raise ValueError("agent executor registry factory objects must be unique")
        captured = tuple(
            AgentExecutorRegistryEntry(
                object_identity,
                AgentExecutorManifestEntry(
                    factory.key,
                    factory.operational_identity,
                    frozenset(factory.supported_modes),
                ),
                factory,
            )
            for object_identity, factory in zip(
                object_identities, factories, strict=True
            )
        )
        if any(not entry.manifest_entry.supported_modes for entry in captured):
            raise ValueError(
                "an agent executor declaring no execution mode can never be selected"
            )
        ordered = tuple(
            sorted(
                captured,
                key=lambda entry: (
                    entry.key.provider_id.value.encode("ascii"),
                    entry.key.executor_revision.value.encode("utf-8"),
                ),
            )
        )
        keys = tuple(entry.key for entry in ordered)
        if len(set(keys)) != len(keys):
            raise ValueError("agent executor registry keys must be unique")
        self._entries = ordered
        self._by_key = dict(zip(keys, ordered, strict=True))

    @property
    def entries(self) -> tuple[AgentExecutorRegistryEntry, ...]:
        return self._entries

    @property
    def manifest(self) -> tuple[AgentExecutorManifestEntry, ...]:
        return tuple(entry.manifest_entry for entry in self._entries)

    @property
    def keys(self) -> frozenset[AgentExecutorKey]:
        return frozenset(self._by_key)

    def factory(self, key: AgentExecutorKey) -> AgentExecutorFactoryV2:
        return self._by_key[key].factory

    def contains(self, key: AgentExecutorKey) -> bool:
        return key in self._by_key
