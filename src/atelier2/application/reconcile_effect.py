from atelier2.contracts.effects import ReconcileCommand, ReconcileCommandSnapshot
from atelier2.ports.effects import EffectReconcileCommander


def reconcile_effect(
    command: ReconcileCommand, commander: EffectReconcileCommander
) -> ReconcileCommandSnapshot:
    return commander.submit(command)
