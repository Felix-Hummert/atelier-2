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
    AgentReceiptResource,
    AnyRunResource,
    NodeAnswerResource,
    NodeDetailResource,
    NodeProvenanceResource,
    NodeRailAttemptResource,
    NodeRailResource,
    NodeResource,
    NodeResourceV2,
    NodeStateName,
    NoWaitingResource,
    NoWaitingResourceV2,
    PublicAttemptStateName,
    RunReceiptResource,
    RunResource,
    RunResourceV2,
    RunResourceV3,
    WaitingInputResource,
    WaitingInputResourceV2,
    WaitingReconciliationResource,
    WaitingReconciliationResourceV2,
    WaitingResource,
    WaitingResourceV2,
)
from atelier2.application.project_node_rail import NodeRailEntry, project_node_rail
from atelier2.contracts.agents import AgentReceiptV2
from atelier2.contracts.run_bindings import RunV2, RunV3
from atelier2.contracts.run_projections import (
    NodeDetail,
    RunProjection,
)
from atelier2.contracts.runs import RunState
from atelier2.contracts.workflows import (
    WaitNode,
    WorkflowGraphV2,
    WorkflowNode,
    WorkflowNodeV2,
)


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
    if isinstance(run, RunV3):
        return _run_resource_v3(projection, run)
    node = cast(
        WorkflowNode | WorkflowNodeV2, projection.graph.node(run.current_node_id)
    )
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
    if run.state is RunState.FAILED:
        raise ValueError("a V1 run cannot end as FAILED")
    return RunResource(
        run_id=run.run_id.value,
        public_run_reference=encode_public_run_reference(run.run_id),
        workflow_revision_hash=run.revision_hash.value,
        state_version=run.state_version,
        state=cast(
            Literal["STARTED", "WAITING_RECONCILIATION", "WAITING_INPUT", "COMPLETED"],
            run.state.value,
        ),
        current_node=cast(NodeResource, node_resource(node)),
        waiting=waiting,
        terminal_hash=None if run.terminal_hash is None else run.terminal_hash.value,
        latest_event_cursor=(
            None
            if run.last_event_sequence == 0
            else encode_event_cursor(run.run_id, run.last_event_sequence)
        ),
    )


def _run_resource_v3(projection: RunProjection, run: RunV3) -> RunResourceV3:
    """One V3 run rendered in its own shape, along the edge its author declared."""
    return RunResourceV3(
        workflow_format_version=3,
        run_id=run.run_id.value,
        public_run_reference=encode_public_run_reference(run.run_id),
        workflow_revision_hash=run.revision_hash.value,
        agent_binding_set_hash=run.binding_set_hash.value,
        run_configuration_revision_hash=run.run_configuration_revision_hash.value,
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
        state=cast(
            Literal["STARTED", "WAITING_INPUT", "COMPLETED", "FAILED"],
            run.state.value,
        ),
        current_node_id=run.current_node_id,
        # A run resource says where the snapshot stands, so no event has
        # overtaken it here; the event stream carries its own rail.
        node_rail=node_rail_resources(project_node_rail(projection, ())),
        terminal_hash=None if run.terminal_hash is None else run.terminal_hash.value,
        latest_event_cursor=(
            None
            if run.last_event_sequence == 0
            else encode_event_cursor(run.run_id, run.last_event_sequence)
        ),
        started_at=None
        if projection.started_at is None
        else projection.started_at.value,
        ended_at=None if projection.ended_at is None else projection.ended_at.value,
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
                # A format-2 attempt can only fail as a process exit; the
                # resource's own literal still rejects any other stored code
                # loudly at projection time, so the cast asserts, not assumes.
                failure_code=cast(
                    Literal["PROCESS_EXITED_UNSUCCESSFULLY"] | None,
                    None
                    if attempt.failure_code is None
                    else attempt.failure_code.value,
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


def node_detail_resource(detail: NodeDetail) -> NodeDetailResource:
    """One node detail on the wire, with every absence kept as an absence."""

    return NodeDetailResource(
        run_id=detail.run_id.value,
        public_run_reference=encode_public_run_reference(detail.run_id),
        node_id=detail.node_id,
        state=cast(NodeStateName, detail.state.value),
        job_base64=None if detail.job is None else encode_canonical_base64(detail.job),
        job_hash=detail.job_hash,
        answer=None
        if detail.answer is None
        else NodeAnswerResource(
            value_base64=encode_canonical_base64(detail.answer.value),
            value_hash=detail.answer.value_hash.value,
        ),
        provenance=None
        if detail.provenance is None
        else NodeProvenanceResource(
            role=detail.provenance.role,
            provider_id=detail.provenance.provider_id,
            model=detail.provenance.model,
            executor_revision=detail.provenance.executor_revision,
            executor_operational_identity=(
                detail.provenance.executor_operational_identity
            ),
            auth_mode=detail.provenance.auth_mode,
            profile_id=detail.provenance.profile_id,
            agent_configuration_revision_hash=(
                detail.provenance.agent_configuration_revision_hash
            ),
            request_hash=detail.provenance.request_hash,
            receipt_hash=detail.provenance.receipt_hash,
        ),
        refusal=detail.refusal,
        started_at=None if detail.started_at is None else detail.started_at.value,
        ended_at=None if detail.ended_at is None else detail.ended_at.value,
    )


def agent_receipt_resource(receipt: AgentReceiptV2) -> AgentReceiptResource:
    """One stored agent receipt, every hash-preimage field, bytes as base64."""

    return AgentReceiptResource(
        request_hash=receipt.request_hash.value,
        node_execution_id=receipt.node_execution_id.value,
        run_id=receipt.run_id.value,
        workflow_revision_hash=receipt.workflow_revision_hash.value,
        node_id=receipt.node_id,
        role=receipt.role.value,
        binding_set_hash=receipt.binding_set_hash.value,
        agent_configuration_revision_hash=(
            receipt.agent_configuration_revision_hash.value
        ),
        auth_profile_revision_hash=receipt.auth_profile_revision_hash.value,
        profile_id=receipt.profile_id,
        revision_number=receipt.revision_number,
        provider_id=receipt.provider_id.value,
        auth_mode=receipt.auth_mode.value,
        model=receipt.model,
        executor_revision=receipt.executor_revision.value,
        executor_operational_identity=receipt.executor_operational_identity.value,
        output_base64=encode_canonical_base64(receipt.output_bytes),
        output_hash=receipt.output_hash.value,
        receipt_hash=receipt.receipt_hash.value,
    )


def run_receipt_resource(items: tuple[AgentReceiptV2, ...]) -> RunReceiptResource:
    return RunReceiptResource(
        items=tuple(agent_receipt_resource(receipt) for receipt in items)
    )
