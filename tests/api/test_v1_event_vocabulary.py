from __future__ import annotations

import pytest

from atelier2.api.models import run_event_resource
from atelier2.api.openapi import EVENT_NAMES
from atelier2.contracts.effects import (
    AdapterOperationalIdentity,
    AdapterRevision,
    CanonicalRequest,
    ConfirmationSource,
    EffectBinding,
    EffectDestination,
    EffectId,
    EffectIntent,
    EffectReceipt,
    EffectResult,
    LogicalEffectKey,
)
from atelier2.contracts.executions import NodeExecutionId, RunEvent, RunEventKind
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.ports.run_events import PersistedRunEvent

RUN_ID = RunId("v1-event-vocabulary")
REVISION_HASH = WorkflowRevisionHash("0" * 64)
NODE_ID = "node"
ATTEMPT_ID = "a" * 64
RECEIPT_KINDS = frozenset(
    {RunEventKind.ACTION_RECONCILIATION_RESOLVED, RunEventKind.ACTION_COMPLETED}
)
CANCELLATION_KINDS = frozenset(
    {
        RunEventKind.AGENT_CANCEL_REQUESTED,
        RunEventKind.AGENT_CANCELLED,
        RunEventKind.AGENT_INTERRUPTED,
    }
)
SETTLED_CANCELLATION_KINDS = CANCELLATION_KINDS - {RunEventKind.AGENT_CANCEL_REQUESTED}


def effect_receipt() -> EffectReceipt:
    binding = EffectBinding(
        logical_key=LogicalEffectKey("v1-event-vocabulary/effect"),
        run_id=RUN_ID,
        workflow_revision_hash=REVISION_HASH,
        adapter_revision=AdapterRevision("adapter-1"),
        destination=EffectDestination("destination"),
        adapter_operational_identity=AdapterOperationalIdentity("installation-1"),
    )
    return EffectReceipt(
        intent=EffectIntent(binding, CanonicalRequest(b"request")),
        effect_id=EffectId("effect-1"),
        result=EffectResult(b"result"),
        confirmation_source=ConfirmationSource.ADAPTER_READBACK,
    )


def v1_projection(kind: RunEventKind) -> PersistedRunEvent:
    """One durable V1 event of the given kind, complete enough to be rendered."""
    receipt = effect_receipt() if kind in RECEIPT_KINDS else None
    payload = receipt.result.payload if receipt is not None else b"1"
    cancelling = kind in CANCELLATION_KINDS
    event = RunEvent(
        RUN_ID,
        REVISION_HASH,
        1,
        NODE_ID,
        NodeExecutionId.for_node(RUN_ID, REVISION_HASH, NODE_ID),
        kind,
        payload,
        receipt_logical_key=None
        if receipt is None
        else receipt.intent.binding.logical_key,
        receipt_result_hash=None if receipt is None else receipt.result.payload_hash,
        agent_attempt_id=ATTEMPT_ID if cancelling else None,
        attempt_ordinal=1 if cancelling else None,
        cancellation_command_id="command-1" if cancelling else None,
        replacement="NONE" if cancelling else None,
        cancellation_disposition=(
            "NEVER_LAUNCHED" if kind in SETTLED_CANCELLATION_KINDS else None
        ),
    )
    return PersistedRunEvent(event, receipt, 1)


UNPUBLISHED_KINDS = tuple(
    sorted(kind for kind in RunEventKind if kind.value not in EVENT_NAMES)
)
ESCAPES_THE_CLOSED_UNION = pytest.mark.xfail(
    strict=True,
    reason=(
        "the V1 event vocabulary is spelled twice and the copies disagree: "
        "openapi.EVENT_NAMES excludes this kind while models.run_event_resource "
        "refuses only AGENT_FAILED, so this kind leaves the closed union through "
        "the fall-through assertion instead of a refusal. #88 gives the set one "
        "owner; this marker comes off with the fix."
    ),
)


@pytest.mark.parametrize(
    "kind", sorted(kind for kind in RunEventKind if kind.value in EVENT_NAMES)
)
def test_every_kind_the_v1_wire_publishes_renders_under_its_own_name(
    kind: RunEventKind,
) -> None:
    resource = run_event_resource(v1_projection(kind))

    assert resource.model_dump()["event"] == kind.value


@pytest.mark.parametrize(
    "kind",
    [
        pytest.param(
            kind,
            id=kind.value,
            marks=[ESCAPES_THE_CLOSED_UNION] if kind in CANCELLATION_KINDS else [],
        )
        for kind in UNPUBLISHED_KINDS
    ],
)
def test_every_kind_the_v1_wire_does_not_publish_is_refused_as_durable_drift(
    kind: RunEventKind,
) -> None:
    with pytest.raises(ValueError):
        run_event_resource(v1_projection(kind))


@pytest.mark.parametrize(
    "kind", sorted(CANCELLATION_KINDS), ids=lambda kind: str(kind.value)
)
def test_a_v1_cancellation_kind_escapes_the_closed_union_instead_of_being_refused(
    kind: RunEventKind,
) -> None:
    """What the refusal above is expected to fail on, named rather than assumed.

    A strict xfail is satisfied by any failure, so this pins the exact exception
    that ends the projection today. #88 makes both tests speak at once: this one
    must be deleted when the fall-through becomes a refusal.
    """

    with pytest.raises(AssertionError, match="closed event union"):
        run_event_resource(v1_projection(kind))
