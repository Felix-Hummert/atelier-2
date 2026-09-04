"""Where a served process learns whether its own auto-redeploy watcher is stuck.

`scripts/auto_redeploy.sh` writes its own tick outcome beside the live
database on every tick that resolves to a success or a failure -- never the
served process itself, which only ever reads it back, through `GET /health`.
"""

from __future__ import annotations

from typing import Protocol

from atelier2.contracts.redeploy_status import RedeployStatus, RedeployStatusMalformed


class RedeployStatusReader(Protocol):
    def __call__(self) -> RedeployStatus | RedeployStatusMalformed | None: ...
