from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from atelier2.contracts.agent_attempts import (
    AgentAttempt,
    AgentAttemptCancellationDisposition,
    AgentAttemptFailureCode,
    AgentAttemptId,
    AgentProcessOwnerId,
    WatchdogGenerationId,
)
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_PROCESS_INPUT_BYTES,
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
class AgentProcessCommand:
    """What one provider asks to be run, whose secrets remain reference-only.

    The executor declares `standard_output_frame_bytes`: the raw stdout frame
    this exact command may produce before supervision refuses it. The port
    owns the field and its validity; the value belongs to the provider whose
    wire format produces the frame, so no provider's number lives here. It is
    a different bound from the durable result bound a decoded execution result
    must satisfy.

    The command carries no working directory. Where a provider runs is an
    attempt's decision, not a provider's, so it arrives as a separate lease.

    Direct process adapters may durably retain this command while proving
    at-most-once launch. Its ordered environment may therefore contain only
    non-secret paths, references and toggles, and it is the child's complete
    environment rather than an overlay on the controller's environment.
    Credential material is handed off through a provider-owned path or OS
    credential channel, never as a value in this record.
    """

    arguments: tuple[str, ...]
    environment: tuple[tuple[str, str], ...] = ()
    standard_input: bytes = b""
    standard_output_frame_bytes: int = field(kw_only=True)

    def __post_init__(self) -> None:
        if not self.arguments or any(not value for value in self.arguments):
            raise ValueError("agent process arguments must be nonempty")
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


# Linux refuses a path longer than PATH_MAX including its terminator, so a
# leased directory that could not be opened is not a directory this port may
# promise. The bound is the port's because durable records derive theirs from
# it: a record whose size bound is derived must know what it may hold.
MAXIMUM_AGENT_ATTEMPT_WORKSPACE_PATH_BYTES = 4_095


@dataclass(frozen=True)
class AgentAttemptWorkspaceLease:
    """One attempt's own scratch working directory, held only in live memory.

    The lease is bound to exactly one `AgentAttemptId`, so two attempts of the
    same node -- an ordinal-1 attempt and its deliberate ordinal-2 replacement
    -- never share a directory. It claims nothing about operating-system
    isolation: it is the directory this attempt owns, holding whatever its
    binding pinned, not a sandbox.

    It carries the directory's own identity, not only its path, because a launch
    happens later and elsewhere: between the attestation and the first process
    there is a window in which a peer of this user can replace the directory the
    path names. The launcher enters the identity -- open, `fstat`, compare, then
    enter through the descriptor it checked -- so the name is never resolved a
    second time.
    """

    attempt_id: AgentAttemptId
    working_directory: Path
    device: int
    inode: int

    def __post_init__(self) -> None:
        if not isinstance(self.attempt_id, AgentAttemptId):
            raise TypeError("agent attempt workspace lease identity must be typed")
        if type(self.device) is not int or type(self.inode) is not int:
            raise TypeError("agent attempt workspace identity must be typed")
        if self.device < 0 or self.inode < 0:
            raise ValueError("agent attempt workspace identity must be nonnegative")
        if not self.working_directory.is_absolute():
            raise ValueError("agent attempt workspace directory must be absolute")
        if (
            len(str(self.working_directory).encode("utf-8"))
            > MAXIMUM_AGENT_ATTEMPT_WORKSPACE_PATH_BYTES
        ):
            raise ValueError("agent attempt workspace directory exceeds the path bound")


@dataclass(frozen=True)
class AgentProcessInvocation:
    """One provider process invocation: a provider's command in one lease."""

    command: AgentProcessCommand
    lease: AgentAttemptWorkspaceLease


class AgentAttemptWorkspaceOwner(Protocol):
    """The provider-neutral owner of every attempt's scratch directory."""

    def preflight(self) -> None:
        """Refuse an unusable scratch root without mutating anything."""
        ...

    def acquire(self, attempt_id: AgentAttemptId) -> AgentAttemptWorkspaceLease:
        """Create this attempt's own directory. Invoke only after its claim won."""
        ...

    def release(self, attempt_id: AgentAttemptId) -> None:
        """Remove this attempt's directory and its contents, idempotently."""
        ...


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
    def prepare_process(self, request: AgentExecutionRequestV2) -> AgentProcessCommand:
        """Prepare a live-only command without starting a child."""
        ...

    def decode_process_completion(
        self, invocation: AgentProcessInvocation, completion: AgentProcessCompletion
    ) -> AgentExecutionResult | AgentExecutionFailure:
        """Decode the answer of exactly this invocation.

        The invocation travels back with the process result because a provider
        may deliver its answer beside the process rather than inside it, and a
        completion carries no identity. Without it such an executor could only
        correlate through its own mutable state -- and one executor object
        serves every attempt on its key, so overlapping attempts would decode
        each other's answers into durable results.
        """
        ...

    def release_credential_channel(self, command: AgentProcessCommand) -> None:
        """Take back the secret channel this invocation handed its provider.

        A provider that reads its credentials from a directory gets one made for
        this command alone, and it is taken back on every path -- success,
        refusal, a claim this call lost, or an exception. It takes the command
        rather than the invocation because the channel is made while the command
        is prepared, which is before the attempt is claimed and therefore before
        any workspace is leased. That is deliberately not the discipline of the
        attempt's workspace: the workspace falls only once the attempt is
        durably terminal, because what a provider left behind is evidence. A
        copy of the operator's credentials is not evidence, and the shortest
        life it can have is the one it gets.
        """
        ...

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

    def contains(self, key: AgentExecutorKey) -> bool:
        return key in self._by_key

    def declared_capabilities(
        self, key: AgentExecutorKey
    ) -> frozenset[AgentExecutionCapability]:
        return self._by_key[key].manifest_entry.declared_capabilities
