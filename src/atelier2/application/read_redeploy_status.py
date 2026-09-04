"""Whether the auto-redeploy watcher is stuck, decided once for every reader.

`GET /health` is the one caller. The port only ever answers the watcher's raw
last tick; this is where that raw tick becomes "the operator should see this
or not" -- a decision, not a port, is what a route may hold.
"""

from __future__ import annotations

from dataclasses import dataclass

from atelier2.contracts.redeploy_status import RedeployStatus, RedeployStatusMalformed
from atelier2.ports.redeploy_status import RedeployStatusReader

# Mirrors scripts/auto_redeploy.sh's own failure_alert_threshold: the same
# third-tick-in-a-row line the watcher itself starts journalling loudly at is
# where a human looking at /health should first learn about it too.
REDEPLOY_BLOCKED_FAILURE_THRESHOLD = 3


@dataclass(frozen=True)
class RedeployNotBlocked:
    """Nothing to tell the operator: no watcher is configured, it has never
    ticked, or its failure streak has not reached the threshold yet."""


@dataclass(frozen=True)
class RedeployBlocked:
    """The watcher has failed the same way at least `REDEPLOY_BLOCKED_FAILURE_THRESHOLD` ticks in a row."""

    blocked_since: str
    reason: str


@dataclass(frozen=True)
class RedeployStatusUnreadable:
    """The watcher's own status file exists but is not the shape it writes."""


type ReadRedeployStatusResult = (
    RedeployNotBlocked | RedeployBlocked | RedeployStatusUnreadable
)


def read_redeploy_status(
    reader: RedeployStatusReader | None,
) -> ReadRedeployStatusResult:
    if reader is None:
        return RedeployNotBlocked()
    match reader():
        case RedeployStatusMalformed():
            return RedeployStatusUnreadable()
        case RedeployStatus() as status:
            return _decide(status)
        case None:
            return RedeployNotBlocked()


def _decide(status: RedeployStatus) -> ReadRedeployStatusResult:
    if status.failure_count < REDEPLOY_BLOCKED_FAILURE_THRESHOLD:
        return RedeployNotBlocked()
    assert status.last_failure_reason is not None
    assert status.last_failure_at is not None
    return RedeployBlocked(
        blocked_since=status.last_failure_at, reason=status.last_failure_reason
    )
