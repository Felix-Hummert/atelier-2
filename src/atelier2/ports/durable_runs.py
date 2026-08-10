from __future__ import annotations

from typing import Protocol

from atelier2.contracts.runs import Run, StartRunRequest


class DurableRunStarter(Protocol):
    def start(self, request: StartRunRequest) -> Run: ...
