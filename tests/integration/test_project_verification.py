"""The project's own manifest decides what verifies it, and says so or refuses.

Two questions live here. What does this project declare -- answered by reading the
manifest, and refused in the manifest's own words where it declares nothing. And
when is that asked -- answered by the attempt, which attests the verification
beside the scratch root: before any provider process, and before the claim that
makes an attempt durable, so a project that declares nothing costs no run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from atelier2.adapters.project_verification import (
    PROJECT_MANIFEST_NAME,
    LocalProjectVerificationRunner,
)
from atelier2.application.execute_agent_attempt import execute_agent_attempt
from atelier2.contracts.agent_attempts import AgentAttempt, AgentAttemptId
from atelier2.contracts.agents import AgentExecutionResult
from atelier2.contracts.executions import AgentAttemptExecution
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.revisions_v3 import PublishedRevisionHash
from atelier2.contracts.tool_grants_v3 import DeclaredToolGrant, ToolGrantCapability
from atelier2.ports.agent_attempts import AgentAttemptClaimResult, AgentAttemptSucceeded
from atelier2.ports.agent_executions import (
    AgentAttemptWorkspaceLease,
    AgentProcessCommand,
    AgentProcessCompletion,
    AgentProcessInvocation,
)
from atelier2.ports.project_verification import (
    ProjectVerificationOutcome,
    ProjectVerificationUnavailable,
    ProjectVerificationUndeclared,
    ToolGrantRedemption,
)
from tests.scenarios.agents import (
    agent_attempt_execution,
    agent_execution_request_v2,
    leased_directory_identity,
    prepared_agent_attempt,
)

THE_GRANT = DeclaredToolGrant(
    PublishedRevisionHash("c3" * 32), ToolGrantCapability.RUN_PROJECT_VERIFICATION
)


def manifest(root: Path, body: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / PROJECT_MANIFEST_NAME).write_text(body, encoding="utf-8")
    return root


def declaring(command: list[str], timeout_seconds: float = 30) -> str:
    return (
        "[tool.atelier2.verification]\n"
        f"command = {json.dumps(command)}\n"
        f"timeout_seconds = {timeout_seconds}\n"
    )


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
    root = manifest(tmp_path / "project", body)

    with pytest.raises(ProjectVerificationUndeclared, match=PROJECT_MANIFEST_NAME):
        LocalProjectVerificationRunner(root).preflight()


@pytest.mark.proves(
    "a-project-that-declares-no-verification-refuses-before-anything-runs"
)
def test_a_root_carrying_no_manifest_at_all_is_refused_by_the_path_it_named(
    tmp_path: Path,
) -> None:
    with pytest.raises(ProjectVerificationUndeclared, match="no project manifest"):
        LocalProjectVerificationRunner(tmp_path / "nowhere").preflight()


def test_the_declared_command_runs_in_the_lease_and_answers_with_its_own_outcome(
    tmp_path: Path,
) -> None:
    root = manifest(
        tmp_path / "project",
        declaring(["/bin/sh", "-c", "pwd; printf ' works'; exit 7"]),
    )
    lease_directory = tmp_path / "lease"
    lease_directory.mkdir()
    lease = leased_directory_identity(AgentAttemptId("a1" * 32), lease_directory)

    outcome = LocalProjectVerificationRunner(root).run(lease)

    assert outcome == ProjectVerificationOutcome(
        ("/bin/sh", "-c", "pwd; printf ' works'; exit 7"),
        7,
        Sha256Hash.of(f"{lease_directory}\n works".encode()),
    )


def test_a_verification_past_its_declared_deadline_is_refused_rather_than_awaited(
    tmp_path: Path,
) -> None:
    root = manifest(tmp_path / "project", declaring(["/bin/sh", "-c", "sleep 30"], 0.2))
    lease_directory = tmp_path / "lease"
    lease_directory.mkdir()
    lease = leased_directory_identity(AgentAttemptId("a2" * 32), lease_directory)

    with pytest.raises(ProjectVerificationUnavailable, match="did not answer"):
        LocalProjectVerificationRunner(root).run(lease)


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

    def preflight(self) -> None:
        self.asked += 1
        raise ProjectVerificationUndeclared("this project states no verification")

    def run(self, lease: AgentAttemptWorkspaceLease) -> ProjectVerificationOutcome:
        raise AssertionError(lease)


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


@pytest.mark.proves(
    "a-project-that-declares-no-verification-refuses-before-anything-runs"
)
def test_an_undeclared_verification_refuses_before_the_attempt_is_claimed() -> None:
    """The refusal costs nothing: no claim, no workspace, no provider process."""
    store = _RefusingStore()
    verifications = _RefusingVerifications()
    executor = _UnlaunchedExecutor()
    workspaces = _CountingWorkspaces()
    execution = agent_attempt_execution(agent_execution_request_v2())

    with pytest.raises(ProjectVerificationUndeclared):
        execute_agent_attempt(
            execution,
            executor,  # type: ignore[arg-type]
            store,  # type: ignore[arg-type]
            _SilentSupervisor(),  # type: ignore[arg-type]
            workspaces,  # type: ignore[arg-type]
            ToolGrantRedemption(THE_GRANT, verifications),
        )

    assert verifications.asked == 1
    assert store.calls == ["prepare"]
    assert (executor.launches, workspaces.acquired) == (0, 0)


class _SilentSupervisor:
    """A supervisor this scenario must never reach."""

    def prepare(self, execution: AgentAttemptExecution) -> AgentAttempt:
        raise AssertionError(execution)
