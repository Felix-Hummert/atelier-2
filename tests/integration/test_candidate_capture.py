"""The one window in which an attempt's work can still be kept.

A lease is deleted the moment an attempt ends, so between the last thing the
attempt did and that deletion there is exactly one moment in which the work can
be kept -- and if it is not kept then, no later owner can invent it back. These
tests assert that window from outside the code implementing it: a succeeded
attempt has a candidate the store still answers for once the directory is gone,
and an attempt whose capture failed says so durably instead of reporting a
success that kept nothing.

There is no shared transaction here and there cannot be one: the candidate lives
in a git repository and the attempt in SQLite. What carries the invariant is the
order -- the ref is written before the attempt is completed -- so what these
tests pin is that order's consequences, never the call sequence producing them.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from atelier2.adapters.candidate_store import (
    CANDIDATE_STORE_DIRECTORY_NAME,
    GitCandidateTreeStore,
)
from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.project_verification import declared_project
from atelier2.application.execute_agent_attempt import execute_agent_attempt
from atelier2.contracts.agent_attempts import (
    AgentAttemptFailureCode,
    AgentAttemptId,
    AgentAttemptState,
)
from atelier2.contracts.agents import AgentExecutionRequestV2, AgentExecutionResult
from atelier2.contracts.executions import AgentAttemptExecution
from atelier2.contracts.project_sources import CandidateTree, ProjectSourcePin
from atelier2.contracts.tool_grants_v3 import (
    DeclaredToolGrant,
    PublishedRevisionHash,
    ToolGrantCapability,
)
from atelier2.ports.agent_attempts import (
    AgentAttemptExecutionOutcome,
    AgentAttemptFailed,
    AgentAttemptSucceeded,
)
from atelier2.ports.agent_executions import (
    AgentAttemptWorkspaceLease,
    AgentProcessCommand,
    AgentProcessCompletion,
    AgentProcessInvocation,
)
from atelier2.ports.candidate_store import (
    CandidateCaptureConflict,
    CandidateStoreUnavailable,
    CandidateTreeStore,
    CandidateTreeUnrepresentable,
)
from tests.integration.test_agent_attempts import attempt_request, attempt_runtime
from tests.integration.test_candidate_store import carried
from tests.scenarios.agents import (
    SCENARIO_PROVIDER_FRAME_BYTES,
    agent_attempt_execution,
    runtime_workspace_owner,
)
from tests.scenarios.projects import declaring_verification, git_project

COMMITTED = "print('committed')\n"
MADE_BY_THE_AGENT = "print('what the agent made')\n"
CHECKED = "checked\n"
WHAT_THE_AGENT_MADE = "made.py"
WHAT_THE_VERIFICATION_MADE = "verified.txt"
A_PROJECT = {"pyproject.toml": "", "src/tool.py": COMMITTED}

THE_GRANT = DeclaredToolGrant(
    PublishedRevisionHash("c3" * 32), ToolGrantCapability.RUN_PROJECT_VERIFICATION
)


def writing(name: str, text: str) -> list[str]:
    """A command leaving exactly one file behind in the directory it runs in."""

    return [
        sys.executable,
        "-c",
        f"import pathlib; pathlib.Path({name!r}).write_text({text!r})",
    ]


@dataclass
class WorkingExecutor:
    """A provider of no particular vendor that leaves its work in the lease."""

    def prepare_process(self, request: AgentExecutionRequestV2) -> AgentProcessCommand:
        del request
        return AgentProcessCommand(
            tuple(writing(WHAT_THE_AGENT_MADE, MADE_BY_THE_AGENT)),
            standard_output_frame_bytes=SCENARIO_PROVIDER_FRAME_BYTES,
        )

    def decode_process_completion(
        self, invocation: AgentProcessInvocation, completion: AgentProcessCompletion
    ) -> AgentExecutionResult:
        del invocation, completion
        return AgentExecutionResult(b'"done"')

    def release_credential_channel(self, command: AgentProcessCommand) -> None:
        del command

    def close(self) -> None:
        return None


@dataclass
class RefusingCandidates:
    """A store that cannot keep this attempt's work, in one named way."""

    refusal: Exception
    asked: list[AgentAttemptId] = field(default_factory=list)

    def capture(
        self, pin: ProjectSourcePin, lease: AgentAttemptWorkspaceLease
    ) -> CandidateTree:
        del pin
        self.asked.append(lease.attempt_id)
        raise self.refusal

    def read(self, attempt_id: AgentAttemptId) -> CandidateTree | None:
        del attempt_id
        return None


class Attempt:
    """One runtime pointed at one project, and the attempts it runs against it."""

    def __init__(self, root: Path) -> None:
        self.checkout = root / "project"
        self.database = root / "atelier.sqlite"
        self.store_path = root / CANDIDATE_STORE_DIRECTORY_NAME
        self.runtime = attempt_runtime(root)
        self.runtime.initialize_storage()
        self.execution: AgentAttemptExecution | None = None

    def close(self) -> None:
        self.runtime.close()

    def project(self, files: Mapping[str, str]) -> ProjectSourcePin:
        return git_project(self.checkout, files)

    @property
    def candidates(self) -> GitCandidateTreeStore:
        """The store as an outside reader opens it, never the one under test."""

        return GitCandidateTreeStore(self.checkout, self.database)

    @property
    def kept(self) -> CandidateTree | None:
        assert self.execution is not None
        return self.candidates.read(self.execution.attempt_id)

    @property
    def durable(self) -> AgentAttemptState:
        assert self.execution is not None
        return (
            DbosAgentAttemptStore(self.runtime.engine)
            .load(self.execution.attempt_id)
            .state
        )

    def run(
        self,
        pin: ProjectSourcePin,
        grant: DeclaredToolGrant | None = None,
        candidates: CandidateTreeStore | None = None,
    ) -> AgentAttemptExecutionOutcome:
        """Run one attempt of this project, with whatever keeps its candidates."""

        project = declared_project(self.checkout, self.database)
        if candidates is not None:
            project = replace(project, candidates=candidates)
        self.execution = agent_attempt_execution(
            attempt_request(self.runtime, "candidate/capture")
        )
        return execute_agent_attempt(
            self.execution,
            WorkingExecutor(),
            DbosAgentAttemptStore(self.runtime.engine),
            self.runtime.agent_process_supervisor,
            runtime_workspace_owner(self.runtime),
            project.pinned(pin, grant),
        )


@pytest.fixture
def attempt(tmp_path: Path) -> Iterator[Attempt]:
    running = Attempt(tmp_path)
    try:
        yield running
    finally:
        running.close()


def test_a_succeeded_attempt_left_its_work_where_the_store_still_reads_it(
    attempt: Attempt,
) -> None:
    """The point of the window: the work outlives the directory it was made in."""

    outcome = attempt.run(attempt.project(A_PROJECT))

    assert isinstance(outcome, AgentAttemptSucceeded)
    kept = attempt.kept
    assert kept is not None
    assert carried(attempt.store_path, kept.tree) == {
        **A_PROJECT,
        WHAT_THE_AGENT_MADE: MADE_BY_THE_AGENT,
    }


def test_the_candidate_carries_what_the_projects_own_verification_ran_against(
    attempt: Attempt,
) -> None:
    """Capture comes after the check, so what is kept is what was verified.

    The declared verification writes into the lease it is given. Were the work
    captured before the redemption, that file could not be in the candidate --
    so its presence is the order itself, asserted through a consequence rather
    than by watching which call came first.
    """

    pin = attempt.project(
        declaring_verification(writing(WHAT_THE_VERIFICATION_MADE, CHECKED))
    )

    outcome = attempt.run(pin, THE_GRANT)

    assert isinstance(outcome, AgentAttemptSucceeded)
    kept = attempt.kept
    assert kept is not None
    assert carried(attempt.store_path, kept.tree)[WHAT_THE_VERIFICATION_MADE] == CHECKED


@pytest.mark.parametrize(
    "refusal",
    [
        CandidateStoreUnavailable("the store did not answer"),
        CandidateCaptureConflict("this attempt is anchored at other work"),
        CandidateTreeUnrepresentable("a nested repository names an unknown commit"),
    ],
    ids=["unavailable", "conflict", "unrepresentable"],
)
def test_an_attempt_whose_work_could_not_be_kept_ends_failed_by_its_own_name(
    attempt: Attempt, refusal: Exception
) -> None:
    """Work lost is work lost -- but it is named, and no run claims it succeeded."""

    outcome = attempt.run(
        attempt.project(A_PROJECT), candidates=RefusingCandidates(refusal)
    )

    assert isinstance(outcome, AgentAttemptFailed)
    assert (
        outcome.attempt.failure_code is AgentAttemptFailureCode.CANDIDATE_CAPTURE_FAILED
    )
    assert attempt.durable is AgentAttemptState.FAILED
    assert attempt.kept is None


def test_a_capture_that_failed_leaves_no_workspace_and_no_armed_attempt(
    attempt: Attempt,
) -> None:
    """The bug this ending exists to prevent: an escape leaving the attempt armed.

    An exception let out after the claim would leave the attempt LAUNCH_ARMED,
    and the replay a recovering workflow performs would report it as possibly
    having run. So the ending is durable and terminal, and the directory is
    released exactly as it is on every other path through this call.
    """

    workspaces = runtime_workspace_owner(attempt.runtime)

    outcome = attempt.run(
        attempt.project(A_PROJECT),
        candidates=RefusingCandidates(CandidateStoreUnavailable("no store")),
    )

    assert isinstance(outcome, AgentAttemptFailed)
    assert list(workspaces.scratch_root.iterdir()) == []
