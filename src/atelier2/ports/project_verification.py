"""Running the verification a project declares for itself, in an attempt's lease.

The command belongs to the project, never to the agent and never to the atelier:
a node that redeems `run-project-verification` asks for *the project's* check, and
what that check is has one owner -- the project manifest. This port is how the
attempt reaches it.

Where it runs is the attempt's decision, which is why `run` takes the lease: the
same split `AgentProcessCommand` already draws between a provider that owns its
command and an attempt that owns its place. The lease claims nothing about
operating-system isolation, and neither does this: the verification runs as this
process's own user, in a blank directory, with the deadline the project declared.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.tool_grants_v3 import DeclaredToolGrant
from atelier2.ports.agent_executions import AgentAttemptWorkspaceLease


class ProjectVerificationUndeclared(Exception):
    """The project this runtime was pointed at declares no verification command."""


class ProjectVerificationUnavailable(Exception):
    """The declared verification could not be run at all, so nothing is claimed."""


@dataclass(frozen=True)
class ProjectVerificationOutcome:
    """What one declared verification ran, how it ended, and what it said."""

    command: tuple[str, ...]
    exit_code: int
    standard_output_hash: Sha256Hash

    def __post_init__(self) -> None:
        if not self.command:
            raise ValueError("a verification outcome names the command that ran")


class ProjectVerificationRunner(Protocol):
    """The provider-neutral owner of the project's own verification command."""

    def preflight(self) -> None:
        """Refuse an undeclared verification without mutating or running anything."""
        ...

    def run(self, lease: AgentAttemptWorkspaceLease) -> ProjectVerificationOutcome:
        """Run the declared command in this attempt's own directory."""
        ...


@dataclass(frozen=True)
class ToolGrantRedemption:
    """One attempt's grant and the runner that redeems it.

    They travel as one value because neither is actionable alone: a grant with no
    runner is a promise nothing keeps, and a runner with no grant is a command
    nobody asked for.
    """

    grant: DeclaredToolGrant
    verifications: ProjectVerificationRunner
