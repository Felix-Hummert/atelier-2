"""Every DBOS workflow id this adapter mints, and nothing else.

A DBOS workflow id is an idempotency key: DBOS refuses a second workflow under an
id it already holds, which is what makes a retried enqueue the same durable work
rather than a duplicate. That makes the id an adapter concern, not a domain one.
The domain identities it is derived from — `NodeExecutionId`, `AgentAttemptId`,
and the logical effect key — stay in `contracts/`, because they are compared and
projected whether DBOS exists or not.

**Three schemas live here, and unifying them is not this module's job.** A framed
digest (`frame(<domain>, …)`), a bare `sha256` of one string, and one plain
concatenation of a prefix and an already-unique id. Every one of them is written
into `workflow_status` rows that exist right now, so a single shared derivation
would restart every durable workflow under an identity nothing recorded. The
three are kept apart deliberately; `tests/domain/test_durable_id_forms.py` pins
what each one produces.
"""

from __future__ import annotations

import hashlib

from atelier2.contracts.agent_attempts import (
    REPLACEMENT_AGENT_ATTEMPT_ORDINAL,
    AgentAttempt,
    AgentAttemptId,
    CancelAgentAttemptRequest,
    stop_command_for,
)
from atelier2.contracts.effects import LogicalEffectKey, ReconcileCommandId
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.hashing import Sha256Hash, frame
from atelier2.contracts.runs import RunId

BOOTSTRAP_WORKFLOW_ID_PREFIX = "atelier2-run-"
FORK_BOOTSTRAP_WORKFLOW_ID_PREFIX = "atelier2-run-fork-"
EFFECT_WORKFLOW_ID_PREFIX = "atelier2-effect-"
RECONCILE_WORKFLOW_ID_PREFIX = "atelier2-reconcile-"
REPLACEMENT_WORKFLOW_ID_PREFIX = "atelier2-agent-replacement-"
RUNNER_LEASE_WORKFLOW_ID_PREFIX = "atelier2-runner-lease-"


def bootstrap_workflow_id_for(run_id: RunId) -> str:
    return (
        BOOTSTRAP_WORKFLOW_ID_PREFIX + hashlib.sha256(run_id.value.encode()).hexdigest()
    )


def fork_bootstrap_workflow_id_for(run_id: RunId) -> str:
    """The driver identity reserved for a successor that may start after entry."""

    return (
        FORK_BOOTSTRAP_WORKFLOW_ID_PREFIX
        + hashlib.sha256(run_id.value.encode()).hexdigest()
    )


def node_workflow_id_for(execution_id: NodeExecutionId) -> str:
    digest = Sha256Hash.of(
        frame("node-workflow-id/v1", execution_id.value.encode("ascii"))
    )
    return f"atelier2-node-{digest.value}"


def answer_workflow_id_for(execution_id: NodeExecutionId) -> str:
    digest = Sha256Hash.of(
        frame("answer-workflow-id/v1", execution_id.value.encode("ascii"))
    )
    return f"atelier2-answer-{digest.value}"


def subworkflow_workflow_id_for(execution_id: NodeExecutionId) -> str:
    digest = Sha256Hash.of(
        frame("subworkflow-workflow-id/v1", execution_id.value.encode("ascii"))
    )
    return f"atelier2-subworkflow-{digest.value}"


def effect_workflow_id_for(logical_key: LogicalEffectKey) -> str:
    return (
        EFFECT_WORKFLOW_ID_PREFIX
        + hashlib.sha256(logical_key.value.encode()).hexdigest()
    )


def action_continuation_workflow_id_for(logical_key: LogicalEffectKey) -> str:
    digest = Sha256Hash.of(
        frame(
            "action-continuation-workflow-id/v1",
            logical_key.value.encode("utf-8"),
        )
    )
    return f"atelier2-action-continuation-{digest.value}"


def reconcile_workflow_id_for(command_id: ReconcileCommandId) -> str:
    return (
        RECONCILE_WORKFLOW_ID_PREFIX
        + hashlib.sha256(command_id.value.encode()).hexdigest()
    )


def cancellation_workflow_id_for(request: CancelAgentAttemptRequest) -> str:
    """The durable workflow that cleans one attempt up under one command.

    The command id takes part, so a second command against the same attempt is a
    second workflow rather than a silently ignored duplicate of the first.
    """

    digest = Sha256Hash.of(
        frame(
            "agent-cancellation-workflow-id/v1",
            request.attempt_id.value.encode("ascii"),
            request.command_id.encode("utf-8"),
        )
    )
    return f"atelier2-agent-cancel-{digest.value}"


def replacement_workflow_id_for(attempt_id: AgentAttemptId) -> str:
    """The durable workflow that runs one replacement attempt.

    The attempt id is already a digest over the execution, the request and the
    ordinal, so hashing it again would buy nothing and change every recorded id.
    """

    return REPLACEMENT_WORKFLOW_ID_PREFIX + attempt_id.value


def runner_lease_workflow_id_for(execution_id: NodeExecutionId, ordinal: int) -> str:
    """The durable workflow that drives one attempt in the single Runner slot.

    The attempt's ordinal takes part because a node's replacement attempt is a
    second turn in that slot, not a repeat of the first that the idempotency key
    would swallow -- the same reason a second round of a node is a second node
    workflow.
    """

    digest = Sha256Hash.of(
        frame(
            "runner-lease-workflow-id/v1",
            execution_id.value.encode("ascii"),
            str(ordinal).encode("ascii"),
        )
    )
    return RUNNER_LEASE_WORKFLOW_ID_PREFIX + digest.value


def driving_workflow_ids(attempt: AgentAttempt) -> tuple[str, ...]:
    """Every durable workflow that can still owe this attempt its next move.

    Which one it is follows from the attempt itself: a cancelled attempt is owed
    its cleanup by the cancellation it carries, a Runner-bound one by the
    lease-slot workflow that drives every Runner Attempt, a replacement by the
    replacement workflow that was enqueued for it, and every other attempt by the
    node workflow of its execution. Naming that here is what lets a restart ask
    whether anything is still driving an attempt at all.

    The Runner slot's own workflow is named beside every one of those, because no
    column ever rules it out (#636). A lease-carried Attempt is durably prepared
    before it binds its generation, so between those two commits the row still
    reads local-process while the slot drives it; and cancelling a lease whose
    launcher already claimed it defers the ending to that same slot workflow
    (`#584`) instead of finishing it there. Naming the slot only where a column
    happens to prove it leaves both of those Attempts looking abandoned to a
    sweep that is about to stop them.

    A wrong name costs nothing: no workflow was ever minted under it, so it
    matches no row and the caller's own status read still decides.
    """

    runner_slot = runner_lease_workflow_id_for(
        attempt.node_execution_id, attempt.attempt_ordinal
    )
    if attempt.cancellation is not None:
        return (cancellation_workflow_id_for(stop_command_for(attempt)), runner_slot)
    if attempt.runner_manifest_id is not None:
        return (runner_slot,)
    if attempt.attempt_ordinal == REPLACEMENT_AGENT_ATTEMPT_ORDINAL:
        return (replacement_workflow_id_for(attempt.attempt_id), runner_slot)
    return (node_workflow_id_for(attempt.node_execution_id), runner_slot)
