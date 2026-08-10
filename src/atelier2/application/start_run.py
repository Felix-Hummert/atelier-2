from __future__ import annotations

from atelier2.contracts.runs import Run, StartRunRequest
from atelier2.ports.durable_runs import DurableRunStarter


def start_run(request: StartRunRequest, starter: DurableRunStarter) -> Run:
    return starter.start(request)
