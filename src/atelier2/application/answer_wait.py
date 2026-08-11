from atelier2.contracts.executions import SubmitWaitAnswerRequest, WaitAnswerSnapshot
from atelier2.ports.durable_runs import WaitAnswerer


def answer_wait(
    request: SubmitWaitAnswerRequest, answerer: WaitAnswerer
) -> WaitAnswerSnapshot:
    return answerer.submit(request)
