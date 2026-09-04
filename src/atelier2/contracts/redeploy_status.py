"""What one auto-redeploy tick's own visibility file can say.

`scripts/auto_redeploy.sh` (the watcher) writes this; `atelier2.adapters.
redeploy_status` reads it from disk; `GET /health` is the one caller. The
shape is shared between the adapter that builds it and the use case that
carries it, so it lives here rather than beside either one's own seam.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RedeployStatus:
    """One auto-redeploy tick's outcome, as the watcher last wrote it."""

    failure_count: int
    last_failure_reason: str | None
    last_failure_at: str | None
    last_success_commit: str | None
    last_success_at: str | None


@dataclass(frozen=True)
class RedeployStatusMalformed:
    """The status file exists but is not the shape the watcher writes.

    Named rather than folded into "no status yet": a malformed file is
    itself an anomaly worth the operator's attention, not one that should
    read the same as an auto-redeploy that has simply never ticked.
    """
