from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa

from atelier2.adapters.dbos.agent_attempt_store import attempt_from_record
from atelier2.adapters.dbos.effect_store import (
    commit_resolution,
    encode_readback,
    intent_snapshot_from_record,
    load_intent,
)
from atelier2.adapters.dbos.run_transitions import load_graph, load_run
from atelier2.adapters.dbos.schema import (
    agent_attempts,
    effect_intents,
    published_revisions,
    run_events,
    runs,
)
from atelier2.adapters.dbos.workflow_ids import driving_workflow_id
from atelier2.contracts.effects import (
    CanonicalRequest,
    EffectAdapterBinding,
    EffectBinding,
    EffectIntent,
    EffectIntentSnapshot,
    EffectIntentState,
    EffectIntentStateVersion,
)
from atelier2.contracts.executions import (
    NodeExecutionId,
    RunEventKind,
    logical_effect_key_for_node,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.contracts.run_bindings import RunBindingConflict
from atelier2.contracts.runs import (
    TERMINAL_RUN_STATES,
    RunId,
    RunState,
    WorkflowRevisionHash,
)
from atelier2.contracts.tool_grants_v3 import (
    DeclaredToolGrant,
    ToolGrantCapability,
    ToolGrantCapabilityNotRedeemed,
    ToolGrantRefused,
    read_tool_grant_document,
    redeems_as_platform_effect,
)
from atelier2.contracts.workflow_formats import WorkflowFormatVersion
from atelier2.contracts.workflows import ActionNode, AgentNode, AgentNodeV2
from atelier2.contracts.workflows_v3 import (
    ANY_ACTION_NODE_KINDS,
    ActionNodeV3,
    AgentNodeV3,
    AnyWorkflowDocument,
    AnyWorkflowDocumentNode,
    WorkflowGraphV3,
)
from atelier2.ports.agent_tool_effects import (
    AgentToolEffectPending,
    redeem_prepared_tool_effect,
)
from atelier2.ports.effects import EffectAdapter


class EffectIntentIdentityConflict(RuntimeError):
    """A logical effect key was retried with different immutable input."""


class RunEffectConflict(RuntimeError):
    """A V1 run cannot prepare this effect against its durable run binding."""


class AgentEffectRedemptionPending(RuntimeError):
    """An agent-node effect readback could name no outcome, so nothing is durable yet.

    Only an authoritative absence licenses this runtime to open the pull
    request, and only a found effect confirms one; an `UNKNOWN` readback is
    neither. Resolving it is the operator reconciliation `ports.effects` already
    owns for every other effect, against live GitHub (`#430`); this slice drives
    a fake platform that can prove its own absence, so it never reaches here and
    fails loud rather than guessing when it would.
    """


def graph_action_intent(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    effect_adapter_binding: EffectAdapterBinding,
) -> EffectIntent:
    run = load_run(session, run_id)
    graph = load_graph(session, revision_hash)
    action = graph.node(run.current_node_id)
    if (
        run.revision_hash != revision_hash
        or run.state is not RunState.STARTED
        or not isinstance(action, ANY_ACTION_NODE_KINDS)
    ):
        raise RunEffectConflict("effect requires the current STARTED Action")
    predecessor = _action_predecessor(graph, action)
    if not isinstance(predecessor, (AgentNode, AgentNodeV2, AgentNodeV3)):
        raise RunEffectConflict("Action predecessor is not an Agent")
    record = session.execute(
        sa.select(run_events.c.payload, run_events.c.payload_hash).where(
            run_events.c.run_id == run_id.value,
            run_events.c.revision_hash == revision_hash.value,
            run_events.c.node_id == predecessor.id,
            run_events.c.event_kind == RunEventKind.AGENT_COMPLETED.value,
        )
    ).one_or_none()
    if record is None:
        raise RunEffectConflict("Action has no durable Agent output")
    payload = bytes(record.payload)
    expected_output = (
        predecessor.output.encode("utf-8")
        if isinstance(predecessor, AgentNode)
        else payload
    )
    if (
        Sha256Hash.of(payload).value != record.payload_hash
        or payload != expected_output
    ):
        raise RunEffectConflict("Action predecessor output binding changed")
    binding = EffectBinding(
        logical_effect_key_for_node(
            run_id, revision_hash, action.id, run.current_round_ordinal
        ),
        run_id,
        revision_hash,
        effect_adapter_binding.adapter_revision,
        effect_adapter_binding.destination,
        effect_adapter_binding.operational_identity,
    )
    return EffectIntent(binding, CanonicalRequest(payload))


def prepare_graph_action(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    effect_adapter_binding: EffectAdapterBinding,
) -> EffectIntentSnapshot:
    intent = graph_action_intent(session, run_id, revision_hash, effect_adapter_binding)
    return prepared_effect_intent(session, intent)


def prepared_effect_intent(session: Any, intent: EffectIntent) -> EffectIntentSnapshot:
    """Record this intent PREPARED, or return the one already durably prepared.

    The logical key is the effect's durable identity: a retry that derives the
    same key from the same immutable node must find the same intent, and a key
    that already belongs to a different intent is a contradiction rather than a
    second preparation. Recording it before any adapter is asked is what lets a
    redemption that never reaches its destination leave a named PREPARED intent
    behind instead of an effect nobody durably asked for.
    """
    existing_record = (
        session.execute(
            sa.select(effect_intents).where(
                effect_intents.c.logical_key == intent.binding.logical_key.value
            )
        )
        .mappings()
        .one_or_none()
    )
    if existing_record is not None:
        snapshot = intent_snapshot_from_record(existing_record)
        if snapshot.intent != intent:
            raise EffectIntentIdentityConflict(
                "logical effect key already belongs to another exact intent"
            )
        return snapshot
    session.execute(
        effect_intents.insert().values(
            logical_key=intent.binding.logical_key.value,
            run_id=intent.binding.run_id.value,
            canonical_request=intent.request.payload,
            request_hash=intent.request.request_hash.value,
            workflow_revision_hash=intent.binding.workflow_revision_hash.value,
            adapter_revision=intent.binding.adapter_revision.value,
            destination_identity=intent.binding.destination.value,
            adapter_operational_identity=(
                intent.binding.adapter_operational_identity.value
            ),
            state=EffectIntentState.PREPARED.value,
            state_version=0,
            reconciliation_owner_command_id=None,
        )
    )
    return EffectIntentSnapshot(
        intent,
        EffectIntentState.PREPARED,
        EffectIntentStateVersion(0),
    )


def read_pinned_tool_grant(
    session: Any, node: AnyWorkflowDocumentNode
) -> DeclaredToolGrant | None:
    """The grant this node pinned, read from the revision the document pins by hash.

    A V3 `tools` entry pins its published revision by that revision's own hash,
    so reading the registry under it is reading exactly what the run
    configuration froze rather than resolving a second time. The bytes are
    immutable and were already read as a grant when the run was bound; a
    registry that cannot answer for them now contradicts a run that has already
    started. Both redemption paths read the grant here -- the binding to carry
    an exec-shaped one, the effect preparation to open a pull request for an
    effect-shaped one -- so the two never read it two different ways.
    """
    if not isinstance(node, AgentNodeV3) or not node.tools:
        return None
    pinned = node.tools[0]
    document = session.scalar(
        sa.select(published_revisions.c.document).where(
            published_revisions.c.kind == RevisionKind.TOOL.value,
            published_revisions.c.revision_hash == pinned.revision,
        )
    )
    if document is None:
        raise RunBindingConflict("the pinned tool revision left the registry")
    grant = read_tool_grant_document(bytes(document))
    if isinstance(grant, ToolGrantRefused):
        raise RunBindingConflict(f"the pinned tool revision is no grant: {grant}")
    return DeclaredToolGrant(PublishedRevisionHash(pinned.revision), grant.capability)


def _effect_shaped_capability_to_open_pr(
    grant: DeclaredToolGrant | None,
) -> ToolGrantCapability | None:
    """The effect-shaped capability this preparation opens a pull request for, or none.

    A missing grant, or one `redeems_as_platform_effect` classifies as
    exec-shaped, prepares no platform effect here -- the exec-shaped grant is
    redeemed inside the attempt's lease instead. An effect-shaped grant that is
    not `open-pr` has no redeemer this preparation performs, so it is refused by
    name here rather than returned as `None` and silently left unprepared: a new
    effect capability then fails loud where its intent would be prepared instead
    of completing a run that opened nothing, exactly as the exec redeemer refuses
    a capability it does not perform. `redeems_as_platform_effect` is the one
    owner of the exec-versus-effect split both redemption paths read.
    """
    if grant is None or not redeems_as_platform_effect(grant.capability):
        return None
    if grant.capability is not ToolGrantCapability.OPEN_PR:
        raise ToolGrantCapabilityNotRedeemed(grant.capability)
    return grant.capability


def first_agent_platform_effect_node(
    session: Any, graph: AnyWorkflowDocument
) -> str | None:
    """The id of the first agent node whose own grant redeems as a platform effect.

    Only a V3 agent node pins a tool grant, and only an effect-shaped grant
    (`_effect_shaped_capability_to_open_pr`) is redeemed against the effect
    adapter after the attempt already succeeded. The redemption has no
    Action-only `WAITING_RECONCILIATION` resting place, so a destination that
    cannot prove absence cannot safely carry it. Admission asks this question at
    run start to refuse such a run before it advances (`#430`/`#431`), reading
    the pinned grant through the same door `prepare_graph_agent_open_pr` reads it.
    """
    if not isinstance(graph, WorkflowGraphV3):
        return None
    for node in graph.nodes:
        if not isinstance(node, AgentNodeV3):
            continue
        if _effect_shaped_capability_to_open_pr(read_pinned_tool_grant(session, node)):
            return node.id
    return None


# DBOS owns this table and these tokens; this module only reads them, to answer
# whether a workflow that would re-run an agent open-pr redemption is still one
# recovery will resume. `atelier2.adapters.dbos`'s other readers of
# `workflow_status` each keep their own narrow copy for the same reason -- a
# shared owner is not a widening either file's scope invites today.
_dbos_workflow_status = sa.table(
    "workflow_status",
    sa.column("workflow_uuid"),
    sa.column("status"),
)
_RECOVERABLE_WORKFLOW_STATUSES = ("PENDING", "ENQUEUED", "DELAYED")
"""The DBOS statuses under which a workflow is still owed its next step, so
recovery on the next launch resumes it rather than leaving it where it lies."""


def agent_open_pr_runs_pending_live_redemption(engine: sa.Engine) -> tuple[RunId, ...]:
    """Every V3 run whose agent open-pr grant could still redeem against live GitHub.

    Admission refuses an agent-authored `open-pr` grant against an adapter that
    cannot prove absence, but that door only guards the runs that instance itself
    starts. A run admitted earlier under an absence-proving adapter can still owe
    its redemption when the same database is reopened against the live adapter,
    and resuming it there ends the run ERROR after it committed COMPLETED
    (`#430`/`#431`). This one pass names exactly those runs so a live-GitHub
    startup can refuse them.

    A run still owes that redemption unless it has durably finished it. The
    redemption runs inside whichever workflow still drives an attempt of the sink
    node -- the node workflow of the original attempt, or the replacement workflow
    of one that was cancelled and replaced -- *after* that workflow committed the
    run COMPLETED, so blocking on the run's state alone misses the crash window
    between the two durable steps: a COMPLETED run one of whose sink-node attempts
    is still driven has not redeemed yet. `_still_owes_live_redemption` is the
    predicate that catches both the in-flight run and that window while letting a
    run whose sink-node attempts have all finished driving serve.
    """
    with engine.connect() as connection:
        blocking: list[RunId] = []
        for record in (
            connection.execute(
                sa.select(
                    runs.c.run_id,
                    runs.c.revision_hash,
                    runs.c.state,
                    runs.c.current_node_id,
                    runs.c.current_round_ordinal,
                ).where(runs.c.workflow_format_version == int(WorkflowFormatVersion.V3))
            )
            .mappings()
            .all()
        ):
            if _still_owes_live_redemption(
                connection, record
            ) and _carries_agent_open_pr_grant(connection, record):
                blocking.append(RunId(str(record["run_id"])))
        return tuple(blocking)


def _still_owes_live_redemption(connection: Any, record: Mapping[Any, Any]) -> bool:
    """Whether this run could still redeem an agent open-pr grant on a next launch.

    A non-terminal run is still advancing -- or parked at input -- toward that
    redemption. A terminal run has committed its ending, but the redemption is
    the last thing the driving workflow of a sink-node attempt does after
    committing COMPLETED; a crash between those two durable steps leaves that
    workflow recoverable and the redemption still owed. A terminal run all of
    whose sink-node attempts have finished driving redeemed once under the
    absence-proving adapter and owes nothing more, so it must not block a later
    live-GitHub start.
    """

    if RunState(str(record["state"])) not in TERMINAL_RUN_STATES:
        return True
    return _current_node_attempt_still_driving(connection, record)


def _current_node_attempt_still_driving(
    connection: Any, record: Mapping[Any, Any]
) -> bool:
    """Whether any attempt of the run's current node is still owed its next move.

    A run reaching COMPLETED leaves its current node at the sink node that
    completed it, and the post-COMPLETED redemption runs inside whichever workflow
    still drives one of that node's attempts. `driving_workflow_id` is the one
    owner of that mapping -- the node workflow for the original attempt, the
    replacement workflow for one that was cancelled and replaced -- so asking it
    for every attempt of the node covers both crash windows without enumerating
    either workflow form here. A recoverable status under any of those ids names a
    redemption recovery would still resume.
    """

    execution_id = NodeExecutionId.for_node(
        RunId(str(record["run_id"])),
        WorkflowRevisionHash(str(record["revision_hash"])),
        str(record["current_node_id"]),
        int(record["current_round_ordinal"]),
    )
    driving_ids = [
        driving_workflow_id(attempt_from_record(attempt_record))
        for attempt_record in connection.execute(
            sa.select(agent_attempts).where(
                agent_attempts.c.node_execution_id == execution_id.value
            )
        )
        .mappings()
        .all()
    ]
    if not driving_ids:
        return False
    found = connection.scalar(
        sa.select(_dbos_workflow_status.c.workflow_uuid)
        .where(
            _dbos_workflow_status.c.workflow_uuid.in_(driving_ids),
            _dbos_workflow_status.c.status.in_(_RECOVERABLE_WORKFLOW_STATUSES),
        )
        .limit(1)
    )
    return found is not None


def _carries_agent_open_pr_grant(connection: Any, record: Mapping[Any, Any]) -> bool:
    """Whether this run's graph pins an agent node that opens its own pull request.

    Read through the same `first_agent_platform_effect_node` door admission reads
    it, so the startup scan and admission never disagree about which grant an
    agent node carries.
    """

    return (
        first_agent_platform_effect_node(
            connection,
            load_graph(connection, WorkflowRevisionHash(str(record["revision_hash"]))),
        )
        is not None
    )


def graph_agent_open_pr_intent(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    round_ordinal: int,
    effect_adapter_binding: EffectAdapterBinding,
) -> EffectIntent | None:
    """The pull-request this agent node's own grant opens, or nothing where none does.

    Which grants open a pull request here is `_effect_shaped_capability_to_open_pr`'s
    decision: a node with no grant or an exec-shaped one prepares nothing, and an
    effect-shaped grant this preparation does not perform is refused by name
    rather than silently left unprepared.

    The request bytes are the node's own durable output, read back from its
    `AGENT_COMPLETED` event rather than trusted from memory, because the same
    provider bytes the run kept are what the pull request must carry. The
    effect binds to the connected repo the adapter names -- not to a
    `project_source` tree-pin -- because a pull request targets a repository,
    not a tree state, so an `open-pr` grant needs no pinned source at all. The
    logical key is derived from this node's own execution identity, which makes
    it deterministic, collision-free, and distinct from the key an Action of the
    same operation would derive from its own node.
    """
    grant = read_pinned_tool_grant(
        session, load_graph(session, revision_hash).node(node_id)
    )
    if _effect_shaped_capability_to_open_pr(grant) is None:
        return None
    execution_id = NodeExecutionId.for_node(
        run_id, revision_hash, node_id, round_ordinal
    )
    binding = EffectBinding(
        logical_effect_key_for_node(run_id, revision_hash, node_id, round_ordinal),
        run_id,
        revision_hash,
        effect_adapter_binding.adapter_revision,
        effect_adapter_binding.destination,
        effect_adapter_binding.operational_identity,
    )
    return EffectIntent(binding, CanonicalRequest(_agent_output(session, execution_id)))


def prepare_graph_agent_open_pr(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    round_ordinal: int,
    effect_adapter_binding: EffectAdapterBinding,
) -> str | None:
    """Prepare this agent node's own pull-request intent, or nothing where none is.

    The logical key it returns is what the redemption step then loads: the two
    are separate durable steps so a PREPARED intent is committed before any
    adapter is asked, exactly as the effect-shaped redemption port requires.
    """
    intent = graph_agent_open_pr_intent(
        session, run_id, revision_hash, node_id, round_ordinal, effect_adapter_binding
    )
    if intent is None:
        return None
    return prepared_effect_intent(session, intent).intent.binding.logical_key.value


def redeem_agent_open_pr(
    session: Any,
    adapter: EffectAdapter,
    logical_key: str,
    revision_hash: str,
) -> str:
    """Redeem one PREPARED agent pull-request intent through the shared adapter.

    Readback runs before create, so a redemption retried after the pull request
    already exists is recognized rather than opened twice. The receipt reaches
    the same `effect_receipts` row an Action's confirmation writes, through the
    same `commit_resolution`, so what an operator reads back is one effect
    whichever authorization opened it. An `UNKNOWN` readback confirms nothing and
    is refused loud rather than guessed at.
    """
    intent = load_intent(session, logical_key, revision_hash)
    redemption = redeem_prepared_tool_effect(intent, adapter)
    if isinstance(redemption, AgentToolEffectPending):
        raise AgentEffectRedemptionPending(
            "an agent open-pr readback named no outcome; nothing may be confirmed yet"
        )
    return commit_resolution(
        session, logical_key, revision_hash, encode_readback(redemption.receipt)
    ).value


def _agent_output(session: Any, execution_id: NodeExecutionId) -> bytes:
    record = session.execute(
        sa.select(run_events.c.payload, run_events.c.payload_hash).where(
            run_events.c.node_execution_id == execution_id.value,
            run_events.c.event_kind == RunEventKind.AGENT_COMPLETED.value,
        )
    ).one_or_none()
    if record is None:
        raise RunEffectConflict("agent open-pr grant has no durable agent output")
    payload = bytes(record.payload)
    if Sha256Hash.of(payload).value != record.payload_hash:
        raise RunEffectConflict("agent output binding changed")
    return payload


def _action_predecessor(
    graph: AnyWorkflowDocument, action: ActionNode | ActionNodeV3
) -> object:
    if isinstance(graph, WorkflowGraphV3):
        if not isinstance(action, ActionNodeV3) or len(action.depends_on) != 1:
            raise RunEffectConflict("Action predecessor is not an Agent")
        predecessor = graph.node(action.depends_on[0])
        if not isinstance(predecessor, AgentNodeV3):
            raise RunEffectConflict("Action predecessor is not an Agent")
        return predecessor
    return graph.predecessor(action.id)
