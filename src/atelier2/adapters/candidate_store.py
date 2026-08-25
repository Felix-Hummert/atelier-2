"""The project's own object store for the work its attempts made.

The store is a bare git repository inside the project's root, created by the
product and written only by it. It is not the operator's checkout: that checkout
is read for what a run was pinned to and never written, and a project keeps
nothing of itself outside its own root. So the candidate of every attempt lands
here, beside the project's database, and a re-clone of the checkout leaves it
untouched.

What is captured is what `git add --all` would have staged in the pinned checkout
itself. That is why the index is seeded from the pinned tree first: a file that is
tracked at the pin but matched by `.gitignore` is content the run must not lose,
and an index that started empty would have dropped it silently. A file the agent
newly created and the project ignores is, here as there, not a change.

The tree is written from the leased directory the attempt worked in, entered
through the identity that lease attested rather than the path it was named by,
and the descriptor that identity was checked on travels into git -- otherwise git
resolves `/proc/self/fd/<n>` against its own descriptors and the check means
nothing.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from atelier2.adapters.leased_directory import entered_leased_directory
from atelier2.adapters.project_source import (
    GitRefused,
    answered_git,
    isolated_git_environment,
)
from atelier2.contracts.agent_attempts import AgentAttemptId
from atelier2.contracts.project_sources import CandidateTree, ProjectSourcePin
from atelier2.ports.agent_executions import AgentAttemptWorkspaceLease
from atelier2.ports.candidate_store import (
    CandidateCaptureConflict,
    CandidateStoreUnavailable,
    CandidateTreeUnrepresentable,
)

CANDIDATE_STORE_DIRECTORY_NAME = ".atelier2-candidates.git"
"""The one name the store has, derived beside the database like the root's parts."""

_CANDIDATE_REF_PREFIX = "refs/atelier/candidates/"

_PINNED_TREE_REF_PREFIX = "refs/atelier/pinned-trees/"
"""What roots a seeded pin, so the store keeps the material a candidate builds on."""

_CAPTURE_INDEX_NAME = "capture.index"
_GITLINK_MODE = "160000"
_STAGED_PATH_SEPARATOR = "\t"

_NO_REF_YET = ""
"""What `git update-ref` calls the old value of a ref that must not exist yet."""


@dataclass(frozen=True, slots=True)
class _LeasedStaging:
    """One capture's way into the lease and the index it stages there.

    The three travel together because none of them means anything alone: the
    descriptor is what makes the path an identity, and the index is what keeps
    this capture out of every other one.
    """

    entered: str
    descriptor: int
    index: Path


class GitCandidateTreeStore:
    """One project's candidates, kept in the project's own bare repository."""

    def __init__(self, project_checkout: Path, database_path: Path) -> None:
        self._project_checkout = project_checkout.resolve()
        self._store = (database_path.parent / CANDIDATE_STORE_DIRECTORY_NAME).resolve()

    def capture(
        self, pin: ProjectSourcePin, lease: AgentAttemptWorkspaceLease
    ) -> CandidateTree:
        self._ensure_store()
        self._seed(pin)
        with tempfile.TemporaryDirectory() as staging:
            written = self._written(pin, lease, Path(staging) / _CAPTURE_INDEX_NAME)
        return self._anchored(CandidateTree(lease.attempt_id, written))

    def read(self, attempt_id: AgentAttemptId) -> CandidateTree | None:
        self._ensure_store()
        standing = self._standing(_CANDIDATE_REF_PREFIX + attempt_id.value)
        return None if standing is None else CandidateTree(attempt_id, standing)

    def _ensure_store(self) -> None:
        """Make the store exist, saying the same thing again if it already does."""

        self._answered(
            ("init", "--bare", "--quiet", str(self._store)),
            working_directory=str(self._store.parent),
            environment=isolated_git_environment(),
            failure=f"the candidate store at {self._store} could not be created",
        )

    def _seed(self, pin: ProjectSourcePin) -> None:
        """Move the pinned tree into the store, so the store stands on its own.

        Only the tree travels, never the history carrying it: a tree names
        subtrees and blobs alone, so this is everything a capture needs and the
        commits the operator's checkout holds stay where they are. Without it the
        store could not read the pin it seeds the index from, and a candidate that
        rewrote only its changed directories would name subtrees nobody has.
        """

        self._answered(
            (
                "push",
                "--quiet",
                str(self._store),
                f"{pin.tree}:{_PINNED_TREE_REF_PREFIX}{pin.tree}",
            ),
            working_directory=str(self._project_checkout),
            environment=isolated_git_environment(),
            failure=(
                f"the pinned tree {pin.tree} could not be moved out of "
                f"{self._project_checkout} into the candidate store"
            ),
        )

    def _written(
        self, pin: ProjectSourcePin, lease: AgentAttemptWorkspaceLease, index: Path
    ) -> str:
        with entered_leased_directory(
            lease.working_directory, lease.device, lease.inode
        ) as (entered, descriptor):
            staging = _LeasedStaging(entered, descriptor, index)
            self._staged(("read-tree", pin.tree), staging)
            self._staged(("add", "--all"), staging)
            self._refuse_nested_repositories(
                self._staged(("ls-files", "--stage", "-z"), staging), lease
            )
            return _one_line(self._staged(("write-tree",), staging))

    def _staged(self, arguments: tuple[str, ...], staging: _LeasedStaging) -> bytes:
        return self._answered(
            arguments,
            working_directory=staging.entered,
            environment=isolated_git_environment(
                GIT_DIR=str(self._store),
                GIT_WORK_TREE=".",
                GIT_INDEX_FILE=str(staging.index),
            ),
            failure="the work standing in the leased workspace could not be staged",
            passed_descriptors=(staging.descriptor,),
        )

    def _refuse_nested_repositories(
        self, staged: bytes, lease: AgentAttemptWorkspaceLease
    ) -> None:
        nested = tuple(
            path for mode, path in _staged_entries(staged) if mode == _GITLINK_MODE
        )
        if nested:
            raise CandidateTreeUnrepresentable(
                f"the workspace of attempt {lease.attempt_id.value} holds the "
                f"nested repositories {', '.join(nested)}, and a candidate naming "
                "commits this store has never seen would name work that is nowhere"
            )

    def _anchored(self, candidate: CandidateTree) -> CandidateTree:
        """Bind the candidate to its attempt, or refuse to restate that attempt.

        The write is a compare-and-set against "no ref yet" rather than a plain
        update, so two captures of one attempt cannot race into one overwriting
        the other. Losing that race is not a failure: the winner is asked what it
        anchored, and only work that differs is a contradiction.
        """

        reference = _CANDIDATE_REF_PREFIX + candidate.attempt_id.value
        try:
            self._in_store(("update-ref", reference, candidate.tree, _NO_REF_YET))
        except CandidateStoreUnavailable as refusal:
            standing = self._standing(reference)
            if standing == candidate.tree:
                return candidate
            if standing is None:
                raise
            raise CandidateCaptureConflict(
                f"attempt {candidate.attempt_id.value} is already anchored at the "
                f"tree {standing}, so anchoring it at {candidate.tree} would make "
                "one attempt claim two different pieces of work"
            ) from refusal
        return candidate

    def _standing(self, reference: str) -> str | None:
        named = _one_line(
            self._in_store(("for-each-ref", "--format=%(objectname)", reference))
        )
        return named or None

    def _in_store(self, arguments: tuple[str, ...]) -> bytes:
        return self._answered(
            arguments,
            working_directory=str(self._store.parent),
            environment=isolated_git_environment(GIT_DIR=str(self._store)),
            failure=f"the candidate store at {self._store} could not be reached",
        )

    def _answered(
        self,
        arguments: tuple[str, ...],
        *,
        working_directory: str,
        environment: dict[str, str],
        failure: str,
        passed_descriptors: tuple[int, ...] = (),
    ) -> bytes:
        try:
            return answered_git(
                arguments,
                working_directory=working_directory,
                environment=environment,
                passed_descriptors=passed_descriptors,
            )
        except GitRefused as error:
            raise CandidateStoreUnavailable(f"{failure}: {error}") from error


def _one_line(answered: bytes) -> str:
    return answered.decode("utf-8", "replace").strip()


def _staged_entries(staged: bytes) -> tuple[tuple[str, str], ...]:
    """The mode and path of every index entry `ls-files --stage -z` listed.

    Read from the NUL-separated form because a path git would otherwise quote --
    one holding a newline, a quote or a non-UTF-8 byte -- is a path a candidate
    still has to be honest about.
    """

    entries = staged.decode("utf-8", "replace").split("\0")
    return tuple(
        (entry.split(" ", 1)[0], entry.split(_STAGED_PATH_SEPARATOR, 1)[1])
        for entry in entries
        if _STAGED_PATH_SEPARATOR in entry
    )
