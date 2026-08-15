"""Projection of a durable run onto its wire schema, in either format version."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, cast

from atelier2.api.projection.workflows import command_resource, node_resource
from atelier2.api.references import (
    encode_canonical_base64,
    encode_event_cursor,
    encode_public_run_reference,
)
from atelier2.api.wire.resources import (
    AgentAttemptCancellationResourceV2,
    AgentAttemptResourceV2,
    AgentBindingResourceV2,
    AnyRunResource,
    NodeRailAttemptResource,
    NodeRailResource,
    NodeResource,
    NodeResourceV2,
    NodeStateName,
    NoWaitingResource,
    NoWaitingResourceV2,
    PublicAttemptStateName,
    RunResource,
    RunResourceV2,
    WaitingInputResource,
    WaitingInputResourceV2,
    WaitingReconciliationResource,
    WaitingReconciliationResourceV2,
    WaitingResource,
    WaitingResourceV2,
)
from atelier2.application.project_node_rail import NodeRailEntry, project_node_rail
from atelier2.contracts.run_bindings import RunV2
from atelier2.contracts.runs import RunState
from atelier2.contracts.workflows import (
    WaitNode,
    WorkflowGraphV2,
    WorkflowNode,
    WorkflowNodeV2,
)
from atelier2.ports.run_queries import RunProjection


def node_rail_resources(
    entries: Sequence[NodeRailEntry],
) -> tuple[NodeRailResource, ...]:
    """The one mapping of the derived rail onto the wire, for a run and an event."""
    return tuple(
        NodeRailResource(
            node_id=entry.node_id,
            state=cast(NodeStateName, entry.state),
            attempt=(
                None
                if entry.attempt is None
                else NodeRailAttemptResource(
                    ordinal=cast(Literal[1, 2], entry.attempt.ordinal),
                    state=cast(PublicAttemptStateName | None, entry.attempt.state),
                )
            ),
        )
        for entry in entries
    )


def run_resource(projection: RunProjection) -> AnyRunResource:
    run = projection.run
    node = projection.graph.node(run.current_node_id)
    if isinstance(run, RunV2):
        return _run_resource_v2(projection, run, node)
    if run.state is RunState.WAITING_INPUT:
        if not isinstance(node, WaitNode):
            raise ValueError("waiting input run does not name a Wait node")
        waiting: WaitingResource = WaitingInputResource(
            type="WAITING_INPUT", node_id=node.id, answer_type=node.answer_type
        )
    elif run.state is RunState.WAITING_RECONCILIATION:
        reconciliation = projection.reconciliation
        if reconciliation is None:
            raise ValueError("waiting reconciliation run has no intent projection")
        intent = reconciliation.intent
        waiting = WaitingReconciliationResource(
            type="WAITING_RECONCILIATION",
            node_id=node.id,
            logical_effect_key=intent.intent.binding.logical_key.value,
            request_hash=intent.intent.request.request_hash.value,
            request_base64=encode_canonical_base64(intent.intent.request.payload),
            intent_state_version=intent.state_version.value,
            pending_command=(
                None
                if reconciliation.pending_command is None
                else command_resource(reconciliation.pending_command.command)
            ),
        )
    else:
        waiting = NoWaitingResource(type="NONE")
    return RunResource(
        run_id=run.run_id.value,
        public_run_reference=encode_public_run_reference(run.run_id),
        workflow_revision_hash=run.revision_hash.value,
        state_version=run.state_version,
        state=run.state.value,
        current_node=cast(NodeResource, node_resource(node)),
        waiting=waiting,
        terminal_hash=None if run.terminal_hash is None else run.terminal_hash.value,
        latest_event_cursor=(
            None
            if run.last_event_sequence == 0
            else encode_event_cursor(run.run_id, run.last_event_sequence)
        ),
    )


def _run_resource_v2(
    projection: RunProjection,
    run: RunV2,
    node: WorkflowNode | WorkflowNodeV2,
) -> RunResourceV2:
    if not isinstance(projection.graph, WorkflowGraphV2):
        raise TypeError("V2 run projection has a V1 workflow graph")
    if run.state is RunState.WAITING_INPUT:
        if not isinstance(node, WaitNode):
            raise ValueError("waiting input V2 run does not name a Wait node")
        waiting: WaitingResourceV2 = WaitingInputResourceV2(
            type="WAITING_INPUT", node_id=node.id, answer_type=node.answer_type
        )
    elif run.state is RunState.WAITING_RECONCILIATION:
        reconciliation = projection.reconciliation
        if reconciliation is None:
            raise ValueError("waiting reconciliation V2 run has no intent projection")
        intent = reconciliation.intent
        waiting = WaitingReconciliationResourceV2(
            type="WAITING_RECONCILIATION",
            node_id=node.id,
            logical_effect_key=intent.intent.binding.logical_key.value,
            request_hash=intent.intent.request.request_hash.value,
            request_base64=encode_canonical_base64(intent.intent.request.payload),
            intent_state_version=intent.state_version.value,
            pending_command=(
                None
                if reconciliation.pending_command is None
                else command_resource(reconciliation.pending_command.command)
            ),
        )
    else:
        waiting = NoWaitingResourceV2(type="NONE")
    return RunResourceV2(
        workflow_format_version=2,
        run_id=run.run_id.value,
        public_run_reference=encode_public_run_reference(run.run_id),
        workflow_revision_hash=run.revision_hash.value,
        agent_binding_set_hash=run.binding_set_hash.value,
        agent_bindings=tuple(
            AgentBindingResourceV2(
                role=binding.role.value,
                agent_configuration_revision_hash=(
                    binding.configuration.revision_hash.value
                ),
                auth_profile_revision_hash=binding.auth_profile.revision_hash.value,
                profile_id=binding.auth_profile.profile_id,
                revision_number=binding.auth_profile.revision_number,
                provider_id=binding.auth_profile.provider_id.value,
                auth_mode=binding.auth_profile.auth_mode.value,
                model=binding.configuration.model,
                executor_revision=binding.configuration.executor_revision.value,
            )
            for binding in run.agent_bindings
        ),
        state_version=run.state_version,
        state=run.state.value,
        current_node=cast(NodeResourceV2, node_resource(node)),
        # A run resource says where the snapshot stands, so no event has
        # overtaken it here; the event stream carries its own rail.
        node_rail=node_rail_resources(project_node_rail(projection, ())),
        agent_attempts=tuple(
            AgentAttemptResourceV2(
                attempt_id=attempt.attempt_id.value,
                node_execution_id=attempt.node_execution_id.value,
                request_hash=attempt.request_hash.value,
                attempt_ordinal=cast(Literal[1, 2], attempt.attempt_ordinal),
                state=cast(PublicAttemptStateName, attempt.state),
                failure_code=(
                    None if attempt.failure_code is None else attempt.failure_code.value
                ),
                cancellation=(
                    None
                    if attempt.cancellation is None
                    else AgentAttemptCancellationResourceV2(
                        command_id=attempt.cancellation.command_id,
                        replacement=attempt.cancellation.replacement.value,
                        redrive_state=attempt.cancellation.redrive_state.value,
                        disposition=(
                            None
                            if attempt.cancellation.disposition is None
                            else attempt.cancellation.disposition.value
                        ),
                    )
                ),
            )
            for attempt in projection.agent_attempts
        ),
        waiting=waiting,
        terminal_hash=None if run.terminal_hash is None else run.terminal_hash.value,
        latest_event_cursor=(
            None
            if run.last_event_sequence == 0
            else encode_event_cursor(run.run_id, run.last_event_sequence)
        ),
    )
