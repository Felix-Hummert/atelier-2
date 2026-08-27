from __future__ import annotations

import pytest

from atelier2.application.fork_run import (
    RunForkCommandConflict,
    RunForkOriginMissing,
    fork_run,
)
from atelier2.contracts.runs import RunId
from atelier2.ports.durable_run_forks import (
    DurableRunForkCommandConflict,
    DurableRunForkOriginMissing,
    DurableRunForkResult,
    ForkRunRequest,
)


class RecordingForker:
    def __init__(self, result: DurableRunForkResult) -> None:
        self.result = result
        self.requests: list[ForkRunRequest] = []

    def fork_run(self, request: ForkRunRequest) -> DurableRunForkResult:
        self.requests.append(request)
        return self.result


@pytest.mark.parametrize(
    ("durable", "expected_type"),
    [
        (DurableRunForkOriginMissing(), RunForkOriginMissing),
        (DurableRunForkCommandConflict(), RunForkCommandConflict),
    ],
)
def test_fork_run_forwards_the_exact_command_and_translates_refusals(
    durable: DurableRunForkResult, expected_type: type[object]
) -> None:
    forker = RecordingForker(durable)

    result = fork_run(RunId("origin"), "retry-1", "review", forker)

    assert isinstance(result, expected_type)
    assert forker.requests == [ForkRunRequest(RunId("origin"), "retry-1", "review")]
