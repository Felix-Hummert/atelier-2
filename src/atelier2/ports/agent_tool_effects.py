"""The redemption shape a future effect-performing tool grant redeems through.

`#431` names the collision: `contracts.tool_grants_v3.ToolGrantCapability` is
synchronous-and-exec-shaped (a command, an exit code, a hash of what it said),
while `contracts.effects` already owns the durable, retryable shape a platform
effect like opening a pull request actually has. Neither shape can honestly
carry the other, so a `RUN_PROJECT_VERIFICATION`-style dispatch could never
redeem an effect-shaped capability without lying about what ran.

Phase 1 of `#431` is inert: no `ToolGrantCapability` member names an effect
yet, so nothing in production calls this port -- it exists so the shape a
Phase 2 effect-shaped capability will redeem through is a contract proved
against a fake adapter now, rather than prose decided while wiring the first
real one. `application.execute_agent_attempt._redeemed` dispatches by
capability today with exactly one case; a capability that redeems through this
port instead of through `ports.project_verification` is Phase 2's own case to
add, together with the `ToolGrantCapability` member that names it.

This module composes `contracts.effects` and `ports.effects` rather than
duplicating them: what it redeems is exactly one `EffectIntent`, through
exactly the `EffectAdapter` protocol `ports.effects` already owns, and what it
answers with is exactly the `EffectReceipt` an effect adapter already
produces. The one thing it adds is the redemption's own read of that shape --
readback before create, and only ever against an intent this call's caller
already holds PREPARED -- not a second effect vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.contracts.effects import (
    ConfirmationSource,
    EffectAbsence,
    EffectIntent,
    EffectReceipt,
    EffectUnknownOutcome,
)
from atelier2.ports.effects import EffectAdapter


@dataclass(frozen=True, slots=True)
class AgentToolEffectDelivered:
    """The effect this redemption asked for exists, however this call learned that.

    A destination that already reports the request performed (readback found
    it) and a destination this call itself just asked to perform the request
    (readback found nothing, so this call executed it) answer the identical
    question a tool redemption asks: did the effect happen, and with what
    result. Which of the two happened is exactly `receipt.confirmation_source`
    -- `ADAPTER_READBACK` or `ADAPTER_EXECUTION` -- so nothing here needs a
    second field to say it again.
    """

    receipt: EffectReceipt


@dataclass(frozen=True, slots=True)
class AgentToolEffectPending:
    """No source here can yet say whether the destination performed this request.

    Only an authoritative absence licenses this call to create the effect; an
    unknown readback licenses nothing. Resolving it is the same operator
    reconciliation `ports.effects.TransactionalEffectReconcileCommander`
    already owns for every other effect -- a tool redemption is one more asker
    of that same durable determination, never a second way to make one.
    """

    unknown: EffectUnknownOutcome


type AgentToolEffectRedemption = AgentToolEffectDelivered | AgentToolEffectPending


def redeem_prepared_tool_effect(
    prepared_intent: EffectIntent, adapter: EffectAdapter
) -> AgentToolEffectRedemption:
    """Redeem one already-PREPARED intent: readback before ever asking to create.

    `prepared_intent` names the exact request this call redeems -- prepared and
    durably recorded by its caller before this call ever runs, exactly as
    `ports.effects` prepares an intent before its bytes are sent, so a call
    that never reaches an adapter (a crash, a retry racing another) leaves a
    named PREPARED intent behind rather than an effect nobody durably asked
    for. Readback runs first so a redemption retried after it already reached
    its destination is recognized rather than repeated: only an authoritative
    absence licenses this call's own execute, and an unknown readback is
    handed back rather than guessed at, exactly as an effect adapter itself is
    never asked to create against an UNKNOWN it did not resolve.
    """
    readback = adapter.readback(prepared_intent)
    prepared_intent.authorize_adapter_readback(readback)
    if isinstance(readback, EffectReceipt):
        return AgentToolEffectDelivered(readback)
    if isinstance(readback, EffectUnknownOutcome):
        return AgentToolEffectPending(readback)
    if isinstance(readback, EffectAbsence):
        performed = adapter.execute(prepared_intent)
        return AgentToolEffectDelivered(
            EffectReceipt(
                intent=prepared_intent,
                effect_id=performed.effect_id,
                result=performed.result,
                confirmation_source=ConfirmationSource.ADAPTER_EXECUTION,
            )
        )
    assert_never(readback)
