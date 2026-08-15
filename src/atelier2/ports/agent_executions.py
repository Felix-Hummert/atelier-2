from __future__ import annotations

from dataclasses import dataclass, field
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
    MAXIMUM_AGENT_PROCESS_STANDARD_OUTPUT_BYTES,
    MAXIMUM_SIGNED_INT64,
    AgentExecutionCapability,
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
    declared_capabilities: frozenset[AgentExecutionCapability]


@dataclass(frozen=True)
class AgentExecutionFailure:
    code: AgentAttemptFailureCode


@dataclass(frozen=True)
class AgentProcessInvocation:
    """One provider process invocation whose secrets remain reference-only.

    The executor declares `standard_output_frame_bytes`: the raw stdout frame
    this exact invocation may produce before supervision refuses it. The port
    owns the field and its validity; the value belongs to the provider whose
    wire format produces the frame, so no provider's number lives here. It is
    a different bound from the durable result bound a decoded execution result
    must satisfy.

    Direct process adapters may durably retain this invocation while proving
    at-most-once launch. Its ordered environment may therefore contain only
    non-secret paths, references and toggles, and it is the child's complete
    environment rather than an overlay on the controller's environment.
    Credential material is handed off through a provider-owned path or OS
    credential channel, never as a value in this record.
    """

    arguments: tuple[str, ...]
    working_directory: Path
    environment: tuple[tuple[str, str], ...] = ()
    standard_input: bytes = b""
    standard_output_frame_bytes: int = field(kw_only=True)

    def __post_init__(self) -> None:
        if not self.arguments or any(not value for value in self.arguments):
            raise ValueError("agent process arguments must be nonempty")
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
        if (
            type(self.standard_output_frame_bytes) is not int
            or self.standard_output_frame_bytes < 1
            or self.standard_output_frame_bytes
            > MAXIMUM_AGENT_PROCESS_STANDARD_OUTPUT_BYTES
        ):
            raise ValueError(
                "agent process standard output frame must fit the portable bound"
            )


@dataclass(frozen=True)
class AgentProcessCompletion:
    return_code: int
    standard_output: bytes
    standard_error: bytes

    def __post_init__(self) -> None:
        if type(self.return_code) is not int:
            raise TypeError("agent process return code must be an integer")
        if not -MAXIMUM_SIGNED_INT64 - 1 <= self.return_code <= MAXIMUM_SIGNED_INT64:
            raise ValueError("agent process return code must fit signed int64")


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
    def declared_capabilities(self) -> frozenset[AgentExecutionCapability]: ...

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
                    frozenset(factory.declared_capabilities),
                ),
                factory,
            )
            for object_identity, factory in zip(
                object_identities, factories, strict=True
            )
        )
        if any(
            not all(
                isinstance(capability, AgentExecutionCapability)
                for capability in entry.manifest_entry.declared_capabilities
            )
            for entry in captured
        ):
            raise TypeError("agent executor capabilities must use their typed contract")
        if any(
            AgentExecutionCapability.HEADLESS
            not in entry.manifest_entry.declared_capabilities
            for entry in captured
        ):
            raise ValueError("every agent executor must declare headless capability")
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

    def declared_capabilities(
        self, key: AgentExecutorKey
    ) -> frozenset[AgentExecutionCapability]:
        return self._by_key[key].manifest_entry.declared_capabilities
