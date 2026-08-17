from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum

from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.hashing import Sha256Hash, frame
from atelier2.contracts.runs import RunId, WorkflowRevisionHash

MAXIMUM_AGENT_FIELD_CHARACTERS = 1_024
MAXIMUM_AGENT_OUTPUT_BYTES_V2 = 49_152
# Current process-frame ceiling, earned by Claude 2.1.221's measured JSON frame;
# each invocation still declares its exact lower limit at the process port.
MAXIMUM_AGENT_PROCESS_STANDARD_OUTPUT_BYTES = 8 * MAXIMUM_AGENT_OUTPUT_BYTES_V2
MAXIMUM_SIGNED_INT64 = 2**63 - 1
MAXIMUM_PROVIDER_ID_CHARACTERS = 64
# The slug's own width, so the pattern, the store's CHECK and the wire cannot
# drift apart: one leading letter and the rest of the allowed characters.
_PROVIDER_ID = re.compile(
    rf"[a-z][a-z0-9._-]{{0,{MAXIMUM_PROVIDER_ID_CHARACTERS - 1}}}"
)


def _require_bounded_text(value: str, owner: str) -> None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MAXIMUM_AGENT_FIELD_CHARACTERS
    ):
        raise ValueError(
            f"{owner} must contain 1..{MAXIMUM_AGENT_FIELD_CHARACTERS} exact characters"
        )


@dataclass(frozen=True)
class AgentRole:
    value: str

    def __post_init__(self) -> None:
        _require_bounded_text(self.value, "agent role")


@dataclass(frozen=True)
class ProviderId:
    value: str

    def __post_init__(self) -> None:
        if _PROVIDER_ID.fullmatch(self.value) is None:
            raise ValueError("provider id must be a lowercase ASCII provider slug")


class AuthMode(StrEnum):
    SUBSCRIPTION = "subscription"
    API_KEY = "api_key"


class AgentExecutionCapability(StrEnum):
    HEADLESS = "headless"
    INTERACTIVE = "interactive"


class AgentConfigurationRevisionFormatVersion(IntEnum):
    V1 = 1
    V2 = 2


class AuthProfileRevisionHash(Sha256Hash):
    """Identity of one public, secret-free authentication profile revision."""


class AgentConfigurationRevisionHash(Sha256Hash):
    """Identity of one immutable model/auth/executor selection."""


class AgentBindingSetHash(Sha256Hash):
    """Identity of the complete role matrix frozen into one V2 run."""


@dataclass(frozen=True)
class AuthProfileRevision:
    profile_id: str
    revision_number: int
    provider_id: ProviderId
    auth_mode: AuthMode
    revision_hash: AuthProfileRevisionHash = field(init=False)

    def __post_init__(self) -> None:
        _require_bounded_text(self.profile_id, "auth profile id")
        if (
            type(self.revision_number) is not int
            or not 1 <= self.revision_number <= MAXIMUM_SIGNED_INT64
        ):
            raise ValueError(
                "auth profile revision number must be a positive signed int64"
            )
        if not isinstance(self.provider_id, ProviderId) or not isinstance(
            self.auth_mode, AuthMode
        ):
            raise TypeError(
                "auth profile provider and mode must use their typed contracts"
            )
        object.__setattr__(
            self,
            "revision_hash",
            AuthProfileRevisionHash.of(
                frame(
                    "auth-profile-revision/v1",
                    self.profile_id.encode("utf-8"),
                    struct.pack(">Q", self.revision_number),
                    self.provider_id.value.encode("ascii"),
                    self.auth_mode.value.encode("ascii"),
                )
            ),
        )


@dataclass(frozen=True)
class AgentConfigurationRevision:
    model: str
    auth_profile_revision_hash: AuthProfileRevisionHash
    executor_revision: AgentExecutorRevision
    requested_capability: AgentExecutionCapability
    revision_format_version: AgentConfigurationRevisionFormatVersion
    revision_hash: AgentConfigurationRevisionHash = field(init=False)

    def __post_init__(self) -> None:
        _require_bounded_text(self.model, "agent model")
        if not isinstance(self.auth_profile_revision_hash, AuthProfileRevisionHash):
            raise TypeError("agent configuration auth hash must be typed")
        if not isinstance(self.executor_revision, AgentExecutorRevision):
            raise TypeError("agent configuration executor revision must be typed")
        _require_bounded_text(self.executor_revision.value, "agent executor revision")
        if not isinstance(self.requested_capability, AgentExecutionCapability):
            raise TypeError("agent configuration capability must be typed")
        if not isinstance(
            self.revision_format_version, AgentConfigurationRevisionFormatVersion
        ):
            raise TypeError("agent configuration format version must be typed")
        if (
            self.revision_format_version is AgentConfigurationRevisionFormatVersion.V1
            and self.requested_capability is not AgentExecutionCapability.HEADLESS
        ):
            raise ValueError("legacy agent configurations require headless capability")
        if self.revision_format_version is AgentConfigurationRevisionFormatVersion.V1:
            framed = frame(
                "agent-configuration-revision/v1",
                self.model.encode("utf-8"),
                self.auth_profile_revision_hash.value.encode("ascii"),
                self.executor_revision.value.encode("utf-8"),
            )
        else:
            framed = frame(
                "agent-configuration-revision/v2",
                self.model.encode("utf-8"),
                self.auth_profile_revision_hash.value.encode("ascii"),
                self.executor_revision.value.encode("utf-8"),
                self.requested_capability.value.encode("ascii"),
            )
        object.__setattr__(
            self,
            "revision_hash",
            AgentConfigurationRevisionHash.of(framed),
        )


@dataclass(frozen=True)
class AgentBinding:
    role: AgentRole
    agent_configuration_revision_hash: AgentConfigurationRevisionHash


@dataclass(frozen=True)
class AgentBindingSet:
    bindings: tuple[AgentBinding, ...]
    binding_set_hash: AgentBindingSetHash = field(init=False)

    def __post_init__(self) -> None:
        ordered = tuple(
            sorted(
                self.bindings, key=lambda binding: binding.role.value.encode("utf-8")
            )
        )
        if len({binding.role for binding in ordered}) != len(ordered):
            raise ValueError("agent binding roles must be unique")
        object.__setattr__(self, "bindings", ordered)
        object.__setattr__(
            self,
            "binding_set_hash",
            AgentBindingSetHash.of(
                frame(
                    "run-agent-bindings/v1",
                    *(
                        frame(
                            "run-agent-binding/v1",
                            binding.role.value.encode("utf-8"),
                            binding.agent_configuration_revision_hash.value.encode(
                                "ascii"
                            ),
                        )
                        for binding in ordered
                    ),
                )
            ),
        )


@dataclass(frozen=True)
class ResolvedAgentBinding:
    role: AgentRole
    configuration: AgentConfigurationRevision
    auth_profile: AuthProfileRevision

    def __post_init__(self) -> None:
        if (
            self.configuration.auth_profile_revision_hash
            != self.auth_profile.revision_hash
        ):
            raise ValueError(
                "resolved agent binding auth revision differs from configuration"
            )


class AgentExecutionRequestHash(Sha256Hash):
    """The immutable fingerprint of one exact logical agent invocation."""


class AgentOutputHash(Sha256Hash):
    """The immutable fingerprint of the exact bytes one agent returned."""


class AgentReceiptHash(Sha256Hash):
    """The immutable fingerprint of one successful agent execution receipt."""


@dataclass(frozen=True)
class AgentExecutorIdentifier:
    value: str

    def __post_init__(self) -> None:
        if self.value == "":
            raise ValueError(f"{type(self).__name__} must be a nonempty string")


class AgentExecutorRevision(AgentExecutorIdentifier):
    """The immutable revision of the executor adapter."""


class AgentExecutorOperationalIdentity(AgentExecutorIdentifier):
    """The stable, non-secret identity of one executor operation."""


@dataclass(frozen=True)
class AgentExecutorBinding:
    adapter_revision: AgentExecutorRevision
    operational_identity: AgentExecutorOperationalIdentity


@dataclass(frozen=True)
class ExactOutputContract:
    output_bytes: bytes


@dataclass(frozen=True)
class AgentExecutionRequest:
    node_execution_id: NodeExecutionId
    run_id: RunId
    workflow_revision_hash: WorkflowRevisionHash
    node_id: str
    job_bytes: bytes
    exact_output: ExactOutputContract
    request_hash: AgentExecutionRequestHash = field(init=False)

    def __post_init__(self) -> None:
        if self.node_id == "":
            raise ValueError("agent request node id must be nonempty")
        if self.job_bytes == b"":
            raise ValueError("agent request job bytes must be nonempty")
        expected_execution = NodeExecutionId.for_node(
            self.run_id, self.workflow_revision_hash, self.node_id
        )
        if self.node_execution_id != expected_execution:
            raise ValueError(
                "agent request execution identity differs from its binding"
            )
        object.__setattr__(
            self,
            "request_hash",
            AgentExecutionRequestHash.of(
                frame(
                    "agent-execution-request/v1",
                    self.node_execution_id.value.encode("ascii"),
                    self.run_id.value.encode("utf-8"),
                    self.workflow_revision_hash.value.encode("ascii"),
                    self.node_id.encode("utf-8"),
                    self.job_bytes,
                    self.exact_output.output_bytes,
                )
            ),
        )


@dataclass(frozen=True)
class AgentExecutionResult:
    output_bytes: bytes


@dataclass(frozen=True)
class AgentExecutionRequestV2:
    node_execution_id: NodeExecutionId
    run_id: RunId
    workflow_revision_hash: WorkflowRevisionHash
    node_id: str
    resolved_binding: ResolvedAgentBinding
    executor_operational_identity: AgentExecutorOperationalIdentity
    job_bytes: bytes
    request_hash: AgentExecutionRequestHash = field(init=False)

    def __post_init__(self) -> None:
        _require_bounded_text(self.node_id, "agent request node id")
        if not self.job_bytes:
            raise ValueError("agent request job bytes must be nonempty")
        expected_execution = NodeExecutionId.for_node(
            self.run_id, self.workflow_revision_hash, self.node_id
        )
        if self.node_execution_id != expected_execution:
            raise ValueError(
                "agent request execution identity differs from its binding"
            )
        if not isinstance(
            self.executor_operational_identity, AgentExecutorOperationalIdentity
        ):
            raise TypeError("executor operational identity must be typed")
        _require_bounded_text(
            self.executor_operational_identity.value,
            "executor operational identity",
        )
        binding = self.resolved_binding
        auth = binding.auth_profile
        configuration = binding.configuration
        object.__setattr__(
            self,
            "request_hash",
            AgentExecutionRequestHash.of(
                frame(
                    "agent-execution-request/v2",
                    self.node_execution_id.value.encode("ascii"),
                    self.run_id.value.encode("utf-8"),
                    self.workflow_revision_hash.value.encode("ascii"),
                    self.node_id.encode("utf-8"),
                    binding.role.value.encode("utf-8"),
                    configuration.revision_hash.value.encode("ascii"),
                    auth.revision_hash.value.encode("ascii"),
                    auth.profile_id.encode("utf-8"),
                    struct.pack(">Q", auth.revision_number),
                    auth.provider_id.value.encode("ascii"),
                    auth.auth_mode.value.encode("ascii"),
                    configuration.model.encode("utf-8"),
                    configuration.executor_revision.value.encode("utf-8"),
                    self.executor_operational_identity.value.encode("utf-8"),
                    self.job_bytes,
                )
            ),
        )


class AgentOutputLimitExceeded(ValueError):
    """A V2 executor returned bytes that cannot be durably projected."""


@dataclass(frozen=True)
class AgentReceiptV2:
    request_hash: AgentExecutionRequestHash
    node_execution_id: NodeExecutionId
    run_id: RunId
    workflow_revision_hash: WorkflowRevisionHash
    node_id: str
    role: AgentRole
    binding_set_hash: AgentBindingSetHash
    agent_configuration_revision_hash: AgentConfigurationRevisionHash
    auth_profile_revision_hash: AuthProfileRevisionHash
    profile_id: str
    revision_number: int
    provider_id: ProviderId
    auth_mode: AuthMode
    model: str
    executor_revision: AgentExecutorRevision
    executor_operational_identity: AgentExecutorOperationalIdentity
    output_bytes: bytes
    output_hash: AgentOutputHash
    receipt_hash: AgentReceiptHash

    def __post_init__(self) -> None:
        _require_bounded_text(self.node_id, "agent receipt node id")
        _require_bounded_text(self.profile_id, "auth profile id")
        _require_bounded_text(self.model, "agent model")
        if self.node_execution_id != NodeExecutionId.for_node(
            self.run_id, self.workflow_revision_hash, self.node_id
        ):
            raise ValueError(
                "agent receipt execution identity differs from its binding"
            )
        if len(self.output_bytes) > MAXIMUM_AGENT_OUTPUT_BYTES_V2:
            raise AgentOutputLimitExceeded(
                f"agent output exceeds {MAXIMUM_AGENT_OUTPUT_BYTES_V2} bytes"
            )
        if self.output_hash != AgentOutputHash.of(self.output_bytes):
            raise ValueError("agent receipt output hash differs from its bytes")
        expected = self.hash_for(
            self.request_hash,
            self.node_execution_id,
            self.run_id,
            self.workflow_revision_hash,
            self.node_id,
            self.role,
            self.binding_set_hash,
            self.agent_configuration_revision_hash,
            self.auth_profile_revision_hash,
            self.profile_id,
            self.revision_number,
            self.provider_id,
            self.auth_mode,
            self.model,
            self.executor_revision,
            self.executor_operational_identity,
            self.output_bytes,
            self.output_hash,
        )
        if self.receipt_hash != expected:
            raise ValueError("agent receipt hash differs from its exact binding")

    @staticmethod
    def hash_for(
        request_hash: AgentExecutionRequestHash,
        node_execution_id: NodeExecutionId,
        run_id: RunId,
        workflow_revision_hash: WorkflowRevisionHash,
        node_id: str,
        role: AgentRole,
        binding_set_hash: AgentBindingSetHash,
        configuration_hash: AgentConfigurationRevisionHash,
        auth_hash: AuthProfileRevisionHash,
        profile_id: str,
        revision_number: int,
        provider_id: ProviderId,
        auth_mode: AuthMode,
        model: str,
        executor_revision: AgentExecutorRevision,
        operational_identity: AgentExecutorOperationalIdentity,
        output_bytes: bytes,
        output_hash: AgentOutputHash,
    ) -> AgentReceiptHash:
        return AgentReceiptHash.of(
            frame(
                "agent-receipt/v2",
                request_hash.value.encode("ascii"),
                node_execution_id.value.encode("ascii"),
                run_id.value.encode("utf-8"),
                workflow_revision_hash.value.encode("ascii"),
                node_id.encode("utf-8"),
                role.value.encode("utf-8"),
                binding_set_hash.value.encode("ascii"),
                configuration_hash.value.encode("ascii"),
                auth_hash.value.encode("ascii"),
                profile_id.encode("utf-8"),
                struct.pack(">Q", revision_number),
                provider_id.value.encode("ascii"),
                auth_mode.value.encode("ascii"),
                model.encode("utf-8"),
                executor_revision.value.encode("utf-8"),
                operational_identity.value.encode("utf-8"),
                output_bytes,
                output_hash.value.encode("ascii"),
            )
        )

    @classmethod
    def for_execution(
        cls,
        request: AgentExecutionRequestV2,
        binding_set_hash: AgentBindingSetHash,
        result: AgentExecutionResult,
    ) -> AgentReceiptV2:
        if len(result.output_bytes) > MAXIMUM_AGENT_OUTPUT_BYTES_V2:
            raise AgentOutputLimitExceeded(
                f"agent output exceeds {MAXIMUM_AGENT_OUTPUT_BYTES_V2} bytes"
            )
        binding = request.resolved_binding
        configuration = binding.configuration
        auth = binding.auth_profile
        output_hash = AgentOutputHash.of(result.output_bytes)
        receipt_hash = cls.hash_for(
            request.request_hash,
            request.node_execution_id,
            request.run_id,
            request.workflow_revision_hash,
            request.node_id,
            binding.role,
            binding_set_hash,
            configuration.revision_hash,
            auth.revision_hash,
            auth.profile_id,
            auth.revision_number,
            auth.provider_id,
            auth.auth_mode,
            configuration.model,
            configuration.executor_revision,
            request.executor_operational_identity,
            result.output_bytes,
            output_hash,
        )
        return cls(
            request.request_hash,
            request.node_execution_id,
            request.run_id,
            request.workflow_revision_hash,
            request.node_id,
            binding.role,
            binding_set_hash,
            configuration.revision_hash,
            auth.revision_hash,
            auth.profile_id,
            auth.revision_number,
            auth.provider_id,
            auth.auth_mode,
            configuration.model,
            configuration.executor_revision,
            request.executor_operational_identity,
            result.output_bytes,
            output_hash,
            receipt_hash,
        )


@dataclass(frozen=True)
class AgentReceipt:
    request_hash: AgentExecutionRequestHash
    node_execution_id: NodeExecutionId
    run_id: RunId
    workflow_revision_hash: WorkflowRevisionHash
    node_id: str
    executor_binding: AgentExecutorBinding
    output_bytes: bytes
    output_hash: AgentOutputHash
    receipt_hash: AgentReceiptHash

    def __post_init__(self) -> None:
        if self.node_id == "":
            raise ValueError("agent receipt node id must be nonempty")
        if self.node_execution_id != NodeExecutionId.for_node(
            self.run_id, self.workflow_revision_hash, self.node_id
        ):
            raise ValueError(
                "agent receipt execution identity differs from its binding"
            )
        expected_output_hash = AgentOutputHash.of(self.output_bytes)
        if self.output_hash != expected_output_hash:
            raise ValueError("agent receipt output hash differs from its bytes")
        expected_receipt_hash = self.hash_for(
            self.request_hash,
            self.node_execution_id,
            self.run_id,
            self.workflow_revision_hash,
            self.node_id,
            self.executor_binding,
            self.output_bytes,
            self.output_hash,
        )
        if self.receipt_hash != expected_receipt_hash:
            raise ValueError("agent receipt hash differs from its exact binding")

    @staticmethod
    def hash_for(
        request_hash: AgentExecutionRequestHash,
        node_execution_id: NodeExecutionId,
        run_id: RunId,
        workflow_revision_hash: WorkflowRevisionHash,
        node_id: str,
        executor_binding: AgentExecutorBinding,
        output_bytes: bytes,
        output_hash: AgentOutputHash,
    ) -> AgentReceiptHash:
        return AgentReceiptHash.of(
            frame(
                "agent-receipt/v1",
                request_hash.value.encode("ascii"),
                node_execution_id.value.encode("ascii"),
                run_id.value.encode("utf-8"),
                workflow_revision_hash.value.encode("ascii"),
                node_id.encode("utf-8"),
                executor_binding.adapter_revision.value.encode("utf-8"),
                executor_binding.operational_identity.value.encode("utf-8"),
                output_bytes,
                output_hash.value.encode("ascii"),
            )
        )

    @classmethod
    def for_execution(
        cls,
        request: AgentExecutionRequest,
        executor_binding: AgentExecutorBinding,
        result: AgentExecutionResult,
    ) -> AgentReceipt:
        output_hash = AgentOutputHash.of(result.output_bytes)
        receipt_hash = cls.hash_for(
            request.request_hash,
            request.node_execution_id,
            request.run_id,
            request.workflow_revision_hash,
            request.node_id,
            executor_binding,
            result.output_bytes,
            output_hash,
        )
        return cls(
            request.request_hash,
            request.node_execution_id,
            request.run_id,
            request.workflow_revision_hash,
            request.node_id,
            executor_binding,
            result.output_bytes,
            output_hash,
            receipt_hash,
        )
