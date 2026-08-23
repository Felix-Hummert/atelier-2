"""The Core-side application driver of one Attempt's Runner lease (`#540` C-3.4).

Mirrors the witness's own hand-composed process
(`tests/witness/runner_candidate_core.py`), production-owned once instead of
repeated by every Core composition: durably prepare the attempt, durably
reuse or mint its one Runner generation, publish the lease
(`atelier2.ports.runner_leases.RunnerLeasePublisher`, `#540` C-3.2), wait for
the launcher's peer material, drive the session to RELEASED over the real
transport (`#540` C-1, reached only through the caller-supplied seam --
`application` may not import `atelier2.adapters.runner_lease_session`
directly), and translate the durably committed evidence into the same
`AgentAttemptExecutionOutcome` union the local-process path
(`execute_agent_attempt`) already answers with.

Inert on its own (`#540` C-3.4): nothing calls this yet. `#540` C-3.6 is the
first production caller, composing `store`/`runner_store`/`core` from one
shared `DbosAgentAttemptStore` and `transport` from
`atelier2.adapters.runner_lease_session.RunnerLeaseSessionListener`.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from atelier2.application.run_runner_session import (
    CoreRunnerSession,
    encode_runner_prepare_payload,
)
from atelier2.contracts.agent_attempts import (
    AgentAttempt,
    AgentAttemptState,
    CancelAgentAttemptRequest,
    RunnerGenerationBinding,
    RunnerGenerationId,
    RunnerInvocationId,
    RunnerTerminalEvidenceHash,
)
from atelier2.contracts.executions import AgentAttemptExecution
from atelier2.contracts.runner_leases import RunnerLeaseId
from atelier2.contracts.runner_manifests import (
    RunnerExecutorCliPin,
    RunnerManifestV1,
    runner_manifest_id,
)
from atelier2.contracts.runner_sessions import RunnerSessionFrame
from atelier2.ports.agent_attempts import (
    AgentAttemptFailed,
    AgentAttemptStore,
    AgentAttemptSucceeded,
    RunnerTerminalEvidenceCommitted,
    RunnerTerminalEvidenceStore,
)
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable
from atelier2.ports.runner_leases import (
    RunnerInvocationChannel,
    RunnerInvocationTimedOut,
    RunnerLeaseAlreadyClaimed,
    RunnerLeasePublisher,
    RunnerLeaseRequest,
    RunnerPeerMaterial,
)


class RunnerAttemptSessionCore(Protocol):
    """The narrow Core mutations a runner-session transport is allowed to ask
    for (`atelier2.ports.runner_sessions.RunnerSessionCore`, restated here so
    `application` never imports the adapter that satisfies it), plus the one
    fact `commit_terminal_record` computes but the wire protocol carries no
    frame for: the terminal commit itself, once RELEASED is reached."""

    def arm(
        self, binding: RunnerGenerationBinding, invocation: RunnerInvocationId
    ) -> None: ...

    def commit_terminal_record(
        self, binding: RunnerGenerationBinding, record: bytes
    ) -> RunnerTerminalEvidenceHash: ...

    def acknowledge(
        self,
        binding: RunnerGenerationBinding,
        evidence_hash: RunnerTerminalEvidenceHash,
        tombstone: bytes,
    ) -> None: ...

    def cancel(self) -> CancelAgentAttemptRequest: ...

    @property
    def committed_evidence(self) -> RunnerTerminalEvidenceCommitted | None: ...


class RunnerAttemptSessionAdvancer(Protocol):
    """Structurally `atelier2.application.run_runner_session.CoreRunnerSession`
    -- declared here, matching
    `atelier2.adapters.runner_core_transport.RunnerSessionAdvancer`, because
    `application` may not import the adapter that names it."""

    def accept(self, frame: RunnerSessionFrame) -> RunnerSessionFrame | None: ...

    def accept_terminal_record(
        self, frame: RunnerSessionFrame
    ) -> RunnerSessionFrame | None: ...

    def cancel(self) -> RunnerSessionFrame: ...


class RunnerLeaseSessionTransport(Protocol):
    """What the composition root's Core-transport seam
    (`atelier2.adapters.runner_lease_session.RunnerLeaseSessionListener`)
    gives this driver: accept the one pinned Runner this Attempt's peer
    material names, and drive its frames to RELEASED."""

    def drive_to_released(
        self,
        binding: RunnerGenerationBinding,
        peer: RunnerPeerMaterial,
        session_for_first_connection: Callable[
            [RunnerInvocationId], RunnerAttemptSessionAdvancer
        ],
        on_started: Callable[[RunnerAttemptSessionAdvancer], RunnerSessionFrame | None]
        | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class RunnerAttemptLeaseMaterial:
    """The composition root's own facts a Runner lease publishes, apart from
    the durable attempt or session: the manifest Core selected, the launcher
    and image this Attempt names, the console's own identity documents a
    launcher's handoff copies verbatim, and the two facts a Runner session
    needs that only an adapter can resolve (`#540` C-3.4) -- the auth
    reference and the CLI conformance pin -- so this driver never has to."""

    manifest: RunnerManifestV1
    runner_image: str
    serve_container: str
    ca_certificate: bytes
    core_certificate: bytes
    core_peer_document: bytes
    auth_reference: str
    cli_pin: RunnerExecutorCliPin


class RunnerAttemptLeasePublishUnavailable(RuntimeError):
    """Publishing this Attempt's lease could not durably complete."""


class RunnerAttemptClaimedAfterInvocationDeadline(RuntimeError):
    """A launcher claimed this lease after this driver gave up waiting on it.

    Named rather than silently reported as a timeout: reporting timeout here
    would claim no Runner ever came for an Attempt a launcher is, in fact,
    now establishing. Converging that race is `#540` Kind #585's job, not
    this phase's -- this driver only refuses to lie about it.
    """


class RunnerAttemptAttestationMismatch(RuntimeError):
    """A launcher's inspect attestation names a manifest Core never bound."""


class RunnerAttemptOutcomeUnmapped(RuntimeError):
    """A released session's terminal attempt state has no execution outcome.

    `AgentAttemptExecutionOutcome` covers succeeded, known-failed, and
    possibly-ran -- the outcomes the local-process path ever answers with.
    A cancelled attempt reaches this driver only through the on-session
    `cancel` hook this phase never wires (`#540` C-3.4 is inert until
    C-3.6), so reaching this is a defect, not a modeled case.
    """


type ExecuteAgentAttemptOnRunnerOutcome = (
    AgentAttemptSucceeded | AgentAttemptFailed | RunnerInvocationTimedOut
)


def execute_agent_attempt_on_runner(
    execution: AgentAttemptExecution,
    store: AgentAttemptStore,
    runner_store: RunnerTerminalEvidenceStore,
    core: RunnerAttemptSessionCore,
    transport: RunnerLeaseSessionTransport,
    leases: RunnerLeasePublisher,
    invocations: RunnerInvocationChannel,
    material: RunnerAttemptLeaseMaterial,
    invocation_deadline_seconds: float,
) -> ExecuteAgentAttemptOnRunnerOutcome:
    """Drive one Attempt's Runner lease from durable preparation to RELEASED.

    `store`, `runner_store`, and `core` must all be composed against the same
    durable attempt store -- `core`'s own wire-level commits and this
    function's `store.prepare`/`runner_store.bind_runner_generation` calls
    have to see one durable truth, or the reuse this function exists to
    guarantee cannot hold. A replayed call for an execution whose generation
    is already durably bound reuses it rather than minting a second one
    (`RunnerBindingConflict` otherwise), and republishes the exact same lease
    request, which `leases.publish` itself answers idempotently -- never a
    second lease for the one Attempt `RunnerLeaseId` already names.
    """

    prepared = store.prepare(execution)
    binding = _generation_binding(execution, prepared, material.manifest)
    runner_store.bind_runner_generation(execution, binding)
    lease_id = RunnerLeaseId(execution.attempt_id.value)
    published = leases.publish(
        RunnerLeaseRequest(
            lease_id,
            binding,
            material.manifest,
            material.runner_image,
            material.serve_container,
            material.ca_certificate,
            material.core_certificate,
            material.core_peer_document,
        )
    )
    if isinstance(published, (DurableWriteUnavailable, DurableStateCorrupt)):
        raise RunnerAttemptLeasePublishUnavailable(published)

    invocation_material = invocations.serve_one_invocation(
        lease_id, invocation_deadline_seconds
    )
    if isinstance(invocation_material, RunnerInvocationTimedOut):
        withdrawal = leases.withdraw(lease_id)
        if isinstance(withdrawal, RunnerLeaseAlreadyClaimed):
            raise RunnerAttemptClaimedAfterInvocationDeadline(lease_id.value)
        return invocation_material
    if invocation_material.inspect_attestation != binding.manifest_id.value:
        raise RunnerAttemptAttestationMismatch(lease_id.value)

    def _session_for_first_connection(
        invocation_id: RunnerInvocationId,
    ) -> CoreRunnerSession:
        return CoreRunnerSession(
            binding,
            core,
            encode_runner_prepare_payload(execution.request, material.auth_reference),
            material.manifest,
            material.auth_reference,
            invocation_id,
            material.cli_pin,
        )

    transport.drive_to_released(
        binding, invocation_material, _session_for_first_connection
    )

    committed = core.committed_evidence
    if committed is None:
        raise AssertionError("a released runner session committed no terminal evidence")
    if committed.completion is not None:
        return AgentAttemptSucceeded(committed.attempt, committed.completion)
    if committed.attempt.state is AgentAttemptState.FAILED:
        return AgentAttemptFailed(committed.attempt)
    raise RunnerAttemptOutcomeUnmapped(committed.attempt.state)


def _generation_binding(
    execution: AgentAttemptExecution,
    prepared_attempt: AgentAttempt,
    manifest: RunnerManifestV1,
) -> RunnerGenerationBinding:
    """This attempt's durable generation, reused rather than reminted.

    A replay of the same execution must never mint a second
    `RunnerGenerationId` for an attempt that already durably bound one --
    `RunnerTerminalEvidenceStore.bind_runner_generation` raises
    `RunnerBindingConflict` on exactly that drift, and a fresh id would also
    publish a second, distinct lease for the same Attempt.
    """

    if prepared_attempt.runner_generation_id is not None:
        if prepared_attempt.runner_manifest_id is None:
            raise AssertionError(
                "agent attempt durably binds a runner generation without a manifest"
            )
        return RunnerGenerationBinding(
            execution.attempt_id,
            execution.request.request_hash,
            prepared_attempt.runner_generation_id,
            prepared_attempt.runner_manifest_id,
        )
    return RunnerGenerationBinding(
        execution.attempt_id,
        execution.request.request_hash,
        RunnerGenerationId(secrets.token_urlsafe(32)),
        runner_manifest_id(manifest),
    )
