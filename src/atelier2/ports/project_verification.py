"""Running the verification a project declares for itself, in an attempt's lease.

The command belongs to the project, never to the agent and never to the atelier:
a node that redeems `run-project-verification` asks for *the project's* check, and
what that check is has one owner -- the project manifest. This port is how the
attempt reaches it.

Which manifest is asked is settled by the pin: the declaration is read out of the
tree one commit names, and the command runs in the tree unpacked from that same
commit, so what a project declares and where it is run can no longer be two
different trees.

Where it runs is the attempt's decision, which is why `run` takes the lease: the
same split `AgentProcessCommand` already draws between a provider that owns its
command and an attempt that owns its place. The lease claims nothing about
operating-system isolation, and neither does this: the verification runs as this
process's own user, in the directory the attempt leased, with the deadline the
project declared.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.project_sources import ProjectSourcePin
from atelier2.contracts.tool_grants_v3 import DeclaredToolGrant
from atelier2.ports.agent_executions import AgentAttemptWorkspaceLease
from atelier2.ports.project_source import ProjectSourceRepository


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

    def preflight(self, pin: ProjectSourcePin) -> None:
        """Refuse an undeclared verification without mutating or running anything."""
        ...

    def run(
        self, pin: ProjectSourcePin, lease: AgentAttemptWorkspaceLease
    ) -> ProjectVerificationOutcome:
        """Run the command the pinned tree declares, in this attempt's directory."""
        ...


@dataclass(frozen=True)
class PinnedProjectSource:
    """One attempt's project: the declared project, and the commit this attempt got.

    The pin is chosen once, when the node's durable binding is composed, so the
    tree the attempt unpacks, the manifest that declares its verification and the
    directory that verification runs in are all one commit. The grant a node
    pinned travels here rather than beside it because a grant without a project
    source is a promise nothing can keep -- there would be no manifest to read it
    from and no tree to run it in. Where a node pinned none, the grant is absent
    and the runner beside it is simply not asked.
    """

    source: ProjectSourceRepository
    verifications: ProjectVerificationRunner
    pin: ProjectSourcePin
    grant: DeclaredToolGrant | None


@dataclass(frozen=True)
class DeclaredProject:
    """The one project this runtime was pointed at: its source, and what verifies it.

    They travel as one value because neither answers alone: a source nothing
    verifies redeems no grant, and a verification with no source has no tree to
    read its declaration from and none to run in.
    """

    source: ProjectSourceRepository
    verifications: ProjectVerificationRunner

    def pinned(
        self, pin: ProjectSourcePin, grant: DeclaredToolGrant | None
    ) -> PinnedProjectSource:
        """What one attempt of this project works in, and redeems there."""

        return PinnedProjectSource(self.source, self.verifications, pin, grant)
