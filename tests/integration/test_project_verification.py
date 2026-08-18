"""The project's own manifest decides what verifies it, and says so or refuses.

Three questions live here. What does this project declare -- answered by reading
the manifest the pinned commit carries, and refused in that manifest's own words
where it declares nothing. Which manifest is that -- answered by the pin alone, so
an edit sitting in the operator's checkout decides nothing about a started run.
And when is it asked -- answered by the attempt, which attests both the pin and
the verification beside the scratch root: before any provider process, and before
the claim that makes an attempt durable, so a project that declares nothing and a
pin that no longer resolves each cost no run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from atelier2.adapters.project_source import LocalGitProjectSource
from atelier2.adapters.project_verification import (
    PROJECT_MANIFEST_NAME,
    LocalProjectVerificationRunner,
)
from atelier2.application.execute_agent_attempt import execute_agent_attempt
from atelier2.contracts.agent_attempts import AgentAttempt, AgentAttemptId
from atelier2.contracts.agents import AgentExecutionResult
from atelier2.contracts.executions import AgentAttemptExecution
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.project_sources import ProjectSourcePin
from atelier2.contracts.revisions_v3 import PublishedRevisionHash
from atelier2.contracts.tool_grants_v3 import DeclaredToolGrant, ToolGrantCapability
from atelier2.ports.agent_attempts import AgentAttemptClaimResult, AgentAttemptSucceeded
from atelier2.ports.agent_executions import (
    AgentAttemptWorkspaceLease,
    AgentProcessCommand,
    AgentProcessCompletion,
    AgentProcessInvocation,
)
from atelier2.ports.project_source import ProjectSourceUnavailable
from atelier2.ports.project_verification import (
    PinnedProjectSource,
    ProjectVerificationOutcome,
    ProjectVerificationUnavailable,
    ProjectVerificationUndeclared,
)
from tests.scenarios.agents import (
    agent_attempt_execution,
    agent_execution_request_v2,
    leased_directory_identity,
    prepared_agent_attempt,
)
from tests.scenarios.projects import (
    declaring_verification,
    git_project,
    write_into_checkout,
)

THE_GRANT = DeclaredToolGrant(
    PublishedRevisionHash("c3" * 32), ToolGrantCapability.RUN_PROJECT_VERIFICATION
)
A_PIN_NO_SOURCE_ANSWERS_FOR = ProjectSourcePin("f0" * 20, "e1" * 20)


def runner_for(root: Path) -> LocalProjectVerificationRunner:
    return LocalProjectVerificationRunner(LocalGitProjectSource(root))


STATES_NO_VERIFICATION: tuple[tuple[str, str], ...] = (
    ("a manifest naming no atelier section", "[tool.pytest]\naddopts = '-q'\n"),
    ("a section naming no verification", "[tool.atelier2]\nname = 'this project'\n"),
    (
        "a verification naming no command",
        "[tool.atelier2.verification]\ntimeout_seconds = 30\n",
    ),
    (
        "a command that is not a command",
        "[tool.atelier2.verification]\ncommand = 'run the tests'\ntimeout_seconds = 30\n",
    ),
    (
        "a command carrying an empty argument",
        '[tool.atelier2.verification]\ncommand = ["/bin/sh", ""]\ntimeout_seconds = 30\n',
    ),
    (
        "a verification naming no deadline",
        '[tool.atelier2.verification]\ncommand = ["/bin/true"]\n',
    ),
    (
        "a deadline that never expires",
        '[tool.atelier2.verification]\ncommand = ["/bin/true"]\ntimeout_seconds = 0\n',
    ),
    ("a manifest that is not a manifest", "[tool.atelier2\n"),
)


@pytest.mark.proves(
    "a-project-that-declares-no-verification-refuses-before-anything-runs"
)
@pytest.mark.parametrize(
    ("label", "body"),
    STATES_NO_VERIFICATION,
    ids=[label for label, _ in STATES_NO_VERIFICATION],
)
def test_a_project_stating_no_verification_is_refused_in_its_manifests_words(
    tmp_path: Path, label: str, body: str
) -> None:
    del label
    root = tmp_path / "project"
    pin = git_project(root, {PROJECT_MANIFEST_NAME: body})

    with pytest.raises(ProjectVerificationUndeclared, match=PROJECT_MANIFEST_NAME):
        runner_for(root).preflight(pin)


@pytest.mark.proves(
    "a-project-that-declares-no-verification-refuses-before-anything-runs"
)
def test_a_commit_carrying_no_manifest_at_all_is_refused_by_the_commit_it_named(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    pin = git_project(root, {"README.md": "a project that never declared one\n"})

    with pytest.raises(ProjectVerificationUndeclared, match="no project manifest"):
        runner_for(root).preflight(pin)


@pytest.mark.proves("what-a-project-declares-and-where-it-runs-are-one-commit")
def test_the_declaration_read_is_the_pinned_commits_and_not_the_checkouts(
    tmp_path: Path,
) -> None:
    """An edit nobody committed decides nothing about a run already pinned."""

    root = tmp_path / "project"
    pin = git_project(root, declaring_verification(["/bin/true"]))
    write_into_checkout(root, {PROJECT_MANIFEST_NAME: "[tool.pytest]\naddopts = ''\n"})

    runner_for(root).preflight(pin)


def test_the_declared_command_runs_in_the_lease_and_answers_with_its_own_outcome(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    pin = git_project(
        root, declaring_verification(["/bin/sh", "-c", "pwd; printf ' works'; exit 7"])
    )
    lease_directory = tmp_path / "lease"
    lease_directory.mkdir()
    lease = leased_directory_identity(AgentAttemptId("a1" * 32), lease_directory)

    outcome = runner_for(root).run(pin, lease)

    assert outcome == ProjectVerificationOutcome(
        ("/bin/sh", "-c", "pwd; printf ' works'; exit 7"),
        7,
        Sha256Hash.of(f"{lease_directory}\n works".encode()),
    )


def test_a_verification_past_its_declared_deadline_is_refused_rather_than_awaited(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    pin = git_project(root, declaring_verification(["/bin/sh", "-c", "sleep 30"], 0.2))
    lease_directory = tmp_path / "lease"
    lease_directory.mkdir()
    lease = leased_directory_identity(AgentAttemptId("a2" * 32), lease_directory)

    with pytest.raises(ProjectVerificationUnavailable, match="did not answer"):
        runner_for(root).run(pin, lease)


@dataclass
class _RefusingStore:
    """A store that records what an attempt asked of it, and refuses to be claimed."""

    calls: list[str] = field(default_factory=list)

    def prepare(self, execution: AgentAttemptExecution) -> AgentAttempt:
        self.calls.append("prepare")
        return prepared_agent_attempt(execution)

    def claim(self, execution: AgentAttemptExecution) -> AgentAttemptClaimResult:
        del execution
        self.calls.append("claim")
        raise AssertionError("a refused verification must not claim an attempt")

    def complete_success(self, *arguments: object) -> AgentAttemptSucceeded:
        raise AssertionError(arguments)


@dataclass
class _RefusingVerifications:
    """A runner standing for a project that states no verification."""

    asked: int = 0

    def preflight(self, pin: ProjectSourcePin) -> None:
        del pin
        self.asked += 1
        raise ProjectVerificationUndeclared("this project states no verification")

    def run(
        self, pin: ProjectSourcePin, lease: AgentAttemptWorkspaceLease
    ) -> ProjectVerificationOutcome:
        raise AssertionError((pin, lease))


@dataclass
class _UnlaunchedExecutor:
    """An executor whose command is prepared and whose process never starts."""

    launches: int = 0

    def prepare_process(self, request: object) -> AgentProcessCommand:
        del request
        return AgentProcessCommand(("/bin/true",), standard_output_frame_bytes=1024)

    def decode_process_completion(
        self, invocation: AgentProcessInvocation, completion: AgentProcessCompletion
    ) -> AgentExecutionResult:
        self.launches += 1
        raise AssertionError((invocation, completion))

    def release_credential_channel(self, command: AgentProcessCommand) -> None:
        del command

    def close(self) -> None:
        return None


@dataclass
class _CountingWorkspaces:
    """A workspace owner that says whether an attempt ever reached its directory."""

    acquired: int = 0

    def preflight(self) -> None:
        return None

    def acquire(self, attempt_id: AgentAttemptId) -> AgentAttemptWorkspaceLease:
        del attempt_id
        self.acquired += 1
        raise AssertionError("a refused verification must not lease a workspace")

    def release(self, attempt_id: AgentAttemptId) -> None:
        del attempt_id


@dataclass
class _RefusedAttempt:
    """One attempt driven until it refuses, and what it cost on the way."""

    store: _RefusingStore = field(default_factory=_RefusingStore)
    executor: _UnlaunchedExecutor = field(default_factory=_UnlaunchedExecutor)
    workspaces: _CountingWorkspaces = field(default_factory=_CountingWorkspaces)

    def drive(self, project: PinnedProjectSource) -> None:
        execute_agent_attempt(
            agent_attempt_execution(agent_execution_request_v2()),
            self.executor,  # type: ignore[arg-type]
            self.store,  # type: ignore[arg-type]
            _SilentSupervisor(),  # type: ignore[arg-type]
            self.workspaces,  # type: ignore[arg-type]
            project,
        )

    @property
    def cost(self) -> tuple[list[str], int, int]:
        """What the refusal spent: store calls, provider launches, leases taken."""

        return (self.store.calls, self.executor.launches, self.workspaces.acquired)


@pytest.mark.proves(
    "a-project-that-declares-no-verification-refuses-before-anything-runs"
)
def test_an_undeclared_verification_refuses_before_the_attempt_is_claimed(
    tmp_path: Path,
) -> None:
    """The refusal costs nothing: no claim, no workspace, no provider process."""
    root = tmp_path / "project"
    pin = git_project(root, declaring_verification(["/bin/true"]))
    verifications = _RefusingVerifications()
    attempt = _RefusedAttempt()

    with pytest.raises(ProjectVerificationUndeclared):
        attempt.drive(
            PinnedProjectSource(
                LocalGitProjectSource(root), verifications, pin, THE_GRANT
            )
        )

    assert verifications.asked == 1
    assert attempt.cost == (["prepare"], 0, 0)


@pytest.mark.proves("a-pin-no-source-can-answer-for-refuses-before-the-claim")
def test_a_pin_this_source_cannot_answer_for_refuses_before_the_attempt_is_claimed(
    tmp_path: Path,
) -> None:
    """A tree nothing can unpack refuses by name rather than running on nothing."""
    root = tmp_path / "project"
    git_project(root, declaring_verification(["/bin/true"]))
    verifications = _RefusingVerifications()
    attempt = _RefusedAttempt()

    with pytest.raises(ProjectSourceUnavailable):
        attempt.drive(
            PinnedProjectSource(
                LocalGitProjectSource(root),
                verifications,
                A_PIN_NO_SOURCE_ANSWERS_FOR,
                THE_GRANT,
            )
        )

    assert verifications.asked == 0
    assert attempt.cost == (["prepare"], 0, 0)


class _SilentSupervisor:
    """A supervisor this scenario must never reach."""

    def prepare(self, execution: AgentAttemptExecution) -> AgentAttempt:
        raise AssertionError(execution)
