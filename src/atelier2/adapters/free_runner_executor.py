from __future__ import annotations

from dataclasses import dataclass

from atelier2.contracts.agents import (
    AgentExecutionCapability,
    AgentExecutionRequestV2,
    AgentExecutionResult,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.contracts.runner_sessions import MAXIMUM_RUNNER_A_TEXT_BYTES
from atelier2.ports.agent_executions import (
    AgentExecutionFailure,
    AgentExecutorKey,
    AgentExecutorV2,
    AgentProcessCommand,
    AgentProcessCompletion,
    AgentProcessInvocation,
)


@dataclass(frozen=True)
class FreeRunnerAuthorization:
    """The fake-free executor receives no credential material."""


class FreeRunnerAuthorizationResolver:
    """Resolve the one candidate authorization from the hashed public profile."""

    def reference_for(self, profile: AuthProfileRevision) -> str:
        return f"urn:atelier2:fake-free-auth:v1:{profile.revision_hash.value}"

    def resolve(
        self, profile: AuthProfileRevision, reference: str
    ) -> FreeRunnerAuthorization:
        if (
            profile.provider_id.value != "fake-free"
            or profile.auth_mode is not AuthMode.API_KEY
            or reference != self.reference_for(profile)
        ):
            raise ValueError("auth-profile-unresolvable")
        return FreeRunnerAuthorization()


class FreeRunnerExecutorFactory:
    """The Core catalogue entry for work that only the isolated Runner executes."""

    @property
    def key(self) -> AgentExecutorKey:
        return AgentExecutorKey(
            ProviderId("fake-free"), AgentExecutorRevision("fake-free/v1")
        )

    @property
    def operational_identity(self) -> AgentExecutorOperationalIdentity:
        return AgentExecutorOperationalIdentity("free-runner-candidate")

    @property
    def declared_capabilities(self) -> frozenset[AgentExecutionCapability]:
        return frozenset((AgentExecutionCapability.HEADLESS,))

    def open(self) -> AgentExecutorV2:
        return _CoreRefusingFreeRunnerExecutor()


class _CoreRefusingFreeRunnerExecutor:
    """Protect the candidate fence if an in-Core process path reaches this key."""

    def prepare_process(self, request: AgentExecutionRequestV2) -> AgentProcessCommand:
        del request
        raise RuntimeError("fake-free execution belongs to the Runner candidate")

    def decode_process_completion(
        self, invocation: AgentProcessInvocation, completion: AgentProcessCompletion
    ) -> AgentExecutionResult | AgentExecutionFailure:
        del invocation, completion
        raise RuntimeError("fake-free execution belongs to the Runner candidate")

    def release_credential_channel(self, command: AgentProcessCommand) -> None:
        del command

    def close(self) -> None:
        return


def refuse_unbound_runner_a_request(request: AgentExecutionRequestV2) -> None:
    """Refuse A-unbound or non-candidate request fields before generation bind."""
    if request.declared_output_schema_bytes is not None:
        raise ValueError("runner-a-output-schema-unbound")
    if request.maximum_assistant_turns is not None:
        raise ValueError("runner-a-turn-limit-unbound")
    if not 1 <= request.round_ordinal <= 2**64 - 1:
        raise ValueError("runner-a-round-out-of-range")
    factory = FreeRunnerExecutorFactory()
    configuration = request.resolved_binding.configuration
    auth = request.resolved_binding.auth_profile
    texts = (
        request.run_id.value,
        request.node_id,
        request.resolved_binding.role.value,
        configuration.model,
        configuration.executor_revision.value,
        configuration.requested_capability.value,
        auth.profile_id,
        auth.provider_id.value,
        auth.auth_mode.value,
        request.executor_operational_identity.value,
    )
    for text_value in texts:
        encoded = text_value.encode("utf-8")
        if (
            encoded.decode("utf-8") != text_value
            or not 1 <= len(encoded) <= MAXIMUM_RUNNER_A_TEXT_BYTES
        ):
            raise ValueError("runner-a-text-oversized")
    if (
        configuration.executor_revision != factory.key.executor_revision
        or request.executor_operational_identity != factory.operational_identity
        or auth.provider_id != factory.key.provider_id
        or auth.auth_mode is not AuthMode.API_KEY
        or configuration.requested_capability not in factory.declared_capabilities
    ):
        raise ValueError("runner-a-executor-unavailable")
