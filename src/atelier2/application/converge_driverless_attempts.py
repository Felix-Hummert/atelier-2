"""Bring every attempt whose driver workflow is gone to a named ending.

A durable run moves because a workflow drives it. When that workflow ends
without moving the attempt it was driving -- it raised, or its process was
taken with it -- the durable runtime has nothing left to recover: a workflow
that reached a terminal status is not pending, and only pending work is
replayed. The attempt then stands armed forever, and a reader of the run is
told "possibly ran" for as long as the store exists.

This is the restart's answer to that. It reads which attempts no live workflow
owes a move to, stops each one under a durable command that says why, and lets
the cancellation path that already exists attest what is left of the process.
The ending is the one the vocabulary already has: an attempt whose owner died
is `INTERRUPTED`, and the run's node rail says `interrupted` rather than
standing still.
"""

from __future__ import annotations

from atelier2.application.cancel_agent_attempt import (
    continue_agent_attempt_cancellation,
)
from atelier2.contracts.agent_attempts import stop_command_for
from atelier2.contracts.pages import MAXIMUM_PAGE_ITEMS, PageLimit
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationAccepted,
    AgentAttemptCancellationStale,
    AgentAttemptCancellationTerminalConflict,
    AgentAttemptStore,
)
from atelier2.ports.agent_executions import (
    AgentAttemptWorkspaceOwner,
    AgentProcessRunner,
)


class DriverlessAttemptUnstoppable(RuntimeError):
    """A restart found an attempt without a driver and could not stop it."""


def converge_driverless_attempts(
    attempts: AgentAttemptStore,
    supervisor: AgentProcessRunner,
    workspaces: AgentAttemptWorkspaceOwner,
) -> None:
    """Stop every driverless attempt without retaining the completed inventory.

    An attempt whose cleanup cannot be attested yet is left under its durable
    stop command rather than forced: the command is enqueued, so the runtime
    redrives it, and this restart does not have to be the one that finishes it.

    A store that refuses the stop for any reason other than the two that mean
    another driver was alive after all raises, because a nonterminal attempt
    that is neither driven nor stoppable is a durable state nobody owns.
    """

    for attempt in attempts.iter_driverless_attempts(PageLimit(MAXIMUM_PAGE_ITEMS)):
        command = stop_command_for(attempt)
        accepted = attempts.request_cancellation(command)
        if isinstance(
            accepted,
            AgentAttemptCancellationStale | AgentAttemptCancellationTerminalConflict,
        ):
            continue
        if not isinstance(accepted, AgentAttemptCancellationAccepted):
            raise DriverlessAttemptUnstoppable(
                f"the store refused to stop driverless attempt "
                f"{attempt.attempt_id.value}: {type(accepted).__name__}"
            )
        continue_agent_attempt_cancellation(command, attempts, supervisor, workspaces)
