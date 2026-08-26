from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from atelier2.contracts.agent_attempts import (
    MAXIMUM_RUNNER_STANDARD_ERROR_BYTES,
    AgentAttempt,
    AgentAttemptCancellationDisposition,
    AgentAttemptFailureCode,
    AgentAttemptId,
    AgentProcessOwnerId,
    WatchdogGenerationId,
)
from atelier2.contracts.agent_transcripts import AttemptTranscript
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_PROCESS_INPUT_BYTES,
    MAXIMUM_AGENT_PROCESS_STANDARD_OUTPUT_BYTES,
    MAXIMUM_SIGNED_INT64,
    UNATTENDED_AGENT_EXECUTION_CAPABILITIES,
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

MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES = MAXIMUM_RUNNER_STANDARD_ERROR_BYTES


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


class AgentExecutorCarrier(StrEnum):
    """Which authority starts one executor key's process (`#540` C-3.6).

    A registration's own fact, not the factory's: the same executor could in
    principle be offered either way, and it is the composition root -- never
    the executor adapter itself -- that decides which authority a served key
    answers under. `LOCAL_PROCESS` is Serve's own `AgentProcessSupervisor`,
    the durable runtime's original and still-default carrier; `RUNNER_LEASE`
    is a per-Attempt Runner container a host launcher establishes from a
    published lease (`atelier2.ports.runner_leases`), and Serve never spawns
    or supervises a process for it directly.
    """

    LOCAL_PROCESS = "local_process"
    RUNNER_LEASE = "runner_lease"


@dataclass(frozen=True)
class AgentExecutorManifestEntry:
    key: AgentExecutorKey
    operational_identity: AgentExecutorOperationalIdentity
    declared_capabilities: frozenset[AgentExecutionCapability]
    carrier: AgentExecutorCarrier = AgentExecutorCarrier.LOCAL_PROCESS


@dataclass(frozen=True)
class AgentExecutionFailure:
    """This process left no answer this executor could use, and what it did leave.

    The transcript is the executor's own reading of what the process wrote --
    the steps it got through, and whatever it printed instead of a usable
    answer. It travels with the failure rather than beside it because only the
    executor knows its provider's wire format, and only the failure it returns
    reaches the seam that can keep the reading durably.
    """

    code: AgentAttemptFailureCode
    transcript: AttemptTranscript | None = None


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
    object_identity: int | None
    manifest_entry: AgentExecutorManifestEntry
    factory: AgentExecutorFactoryV2 | None

    @property
    def key(self) -> AgentExecutorKey:
        return self.manifest_entry.key


@dataclass(frozen=True)
class AgentExecutorRegistration:
    """One declared executor and the factory that can currently start it."""

    manifest_entry: AgentExecutorManifestEntry
    factory: AgentExecutorFactoryV2 | None

    @classmethod
    def startable(
        cls,
        factory: AgentExecutorFactoryV2,
        carrier: AgentExecutorCarrier = AgentExecutorCarrier.LOCAL_PROCESS,
    ) -> AgentExecutorRegistration:
        return cls(
            AgentExecutorManifestEntry(
                factory.key,
                factory.operational_identity,
                frozenset(factory.declared_capabilities),
                carrier,
            ),
            factory,
        )

    @classmethod
    def unavailable(
        cls,
        factory: AgentExecutorFactoryV2,
        carrier: AgentExecutorCarrier = AgentExecutorCarrier.LOCAL_PROCESS,
    ) -> AgentExecutorRegistration:
        return cls(
            AgentExecutorManifestEntry(
                factory.key,
                factory.operational_identity,
                frozenset(factory.declared_capabilities),
                carrier,
            ),
            None,
        )


class AgentExecutorRegistry:
    """Immutable host registry for declared executors and current startability."""

    def __init__(
        self,
        registrations: tuple[
            AgentExecutorFactoryV2 | AgentExecutorRegistration, ...
        ] = (),
    ) -> None:
        factories = tuple(
            registration.factory
            if isinstance(registration, AgentExecutorRegistration)
            else registration
            for registration in registrations
        )
        object_identities = tuple(
            id(factory) for factory in factories if factory is not None
        )
        if len(set(object_identities)) != len(object_identities):
            raise ValueError("agent executor registry factory objects must be unique")
        captured_registrations = tuple(
            registration
            if isinstance(registration, AgentExecutorRegistration)
            else AgentExecutorRegistration.startable(registration)
            for registration in registrations
        )
        captured = tuple(
            AgentExecutorRegistryEntry(
                (None if registration.factory is None else id(registration.factory)),
                registration.manifest_entry,
                registration.factory,
            )
            for registration in captured_registrations
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
            not (
                entry.manifest_entry.declared_capabilities
                & UNATTENDED_AGENT_EXECUTION_CAPABILITIES
            )
            for entry in captured
        ):
            raise ValueError(
                "every agent executor must declare an unattended capability"
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

    def contains(self, key: AgentExecutorKey) -> bool:
        return key in self._by_key

    def declared_capabilities(
        self, key: AgentExecutorKey
    ) -> frozenset[AgentExecutionCapability]:
        return self._by_key[key].manifest_entry.declared_capabilities

    def carrier(self, key: AgentExecutorKey) -> AgentExecutorCarrier:
        return self._by_key[key].manifest_entry.carrier

    def is_startable(
        self, key: AgentExecutorKey, capability: AgentExecutionCapability
    ) -> bool:
        entry = self._by_key.get(key)
        return (
            entry is not None
            and entry.factory is not None
            and capability in entry.manifest_entry.declared_capabilities
        )
