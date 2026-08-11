from __future__ import annotations

from typing import Protocol

from atelier2.contracts.executions import SubmitWaitAnswerRequest, WaitAnswerSnapshot
from atelier2.contracts.runs import Run, StartRunRequest


class DurableRunStarter(Protocol):
    def start(self, request: StartRunRequest) -> Run: ...


class WaitAnswerer(Protocol):
    def submit(self, request: SubmitWaitAnswerRequest) -> WaitAnswerSnapshot: ...
