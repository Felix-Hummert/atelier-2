from atelier2.contracts.effects import EffectIntent, EffectIntentSnapshot
from atelier2.ports.effects import DurableRunAdvancer


def advance_run(
    intent: EffectIntent, advancer: DurableRunAdvancer
) -> EffectIntentSnapshot:
    return advancer.advance(intent)
