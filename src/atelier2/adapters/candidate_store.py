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

Nothing here speaks a git transport. The pinned material is packed out of the
checkout and indexed into the store as objects, because a `push` would start a
`receive-pack` at one end and a `pre-push` at the other, and both are programs of
the operator's choosing running inside a run that never asked for them.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import IO

from atelier2.adapters.leased_directory import entered_leased_directory
from atelier2.adapters.project_source import (
    NO_GIT_TEMPLATE,
    GitRefused,
    LeasedIndex,
    answered_git,
    answered_in_lease,
    isolated_git_environment,
    object_format_of,
)
from atelier2.contracts.agent_attempts import AgentAttemptId
from atelier2.contracts.project_sources import (
    CandidateTree,
    GitObjectFormat,
    ProjectSourcePin,
)
from atelier2.ports.agent_executions import AgentAttemptWorkspaceLease
from atelier2.ports.candidate_store import (
    CandidateCaptureConflict,
    CandidateStoreUnavailable,
    CandidateTreeUnrepresentable,
)

CANDIDATE_STORE_DIRECTORY_NAME = ".atelier2-candidates.git"
"""The one name the store has, derived beside the database like the root's parts."""

CANDIDATE_REF_PREFIX = "refs/atelier/candidates/"
"""Where one attempt's work is anchored, under that attempt's own identity."""

PINNED_TREE_REF_PREFIX = "refs/atelier/pinned-trees/"
"""What roots a seeded pin, so the store keeps the material a candidate builds on."""

_LOCK_HANDOVER_LOOKS = 5
_LOCK_HANDOVER_PAUSE_SECONDS = 0.05
"""How long a refused writer waits for the one holding the ref lock to finish.

git writes a ref by taking a `.lock`, writing into it, then renaming it into
place: a writer refused inside that window sees no value where the winner is
about to put one, a few milliseconds away. Four short pauses are a fifth of a
second in all -- long enough for a local rename, short enough that a ref nobody
is writing fails promptly instead of holding a capture open.
"""

_CAPTURE_INDEX_NAME = "capture.index"
_PACKED_PIN_BASE_NAME = "pinned-tree"
_WANTED_OBJECTS_NAME = "wanted-objects"
_GITLINK_MODE = "160000"
_STAGED_PATH_SEPARATOR = "\t"

_UNFINISHED_STORE_PREFIX = ".atelier2-candidates-unfinished-"
"""What a store being built is called, so a half-made one is never mistaken for one."""

_NO_REF_YET = ""
"""What `git update-ref` calls the old value of a ref that must not exist yet."""


class GitCandidateTreeStore:
    """One project's candidates, kept in the project's own bare repository."""

    def __init__(self, project_checkout: Path, database_path: Path) -> None:
        self._project_checkout = project_checkout.resolve()
        self._store = (database_path.parent / CANDIDATE_STORE_DIRECTORY_NAME).resolve()

    def capture(
        self, pin: ProjectSourcePin, lease: AgentAttemptWorkspaceLease
    ) -> CandidateTree:
        self._ensure_store()
        with tempfile.TemporaryDirectory() as staging:
            self._seed(pin, Path(staging))
            written = self._written(pin, lease, Path(staging) / _CAPTURE_INDEX_NAME)
        return self._anchored(CandidateTree(lease.attempt_id, written))

    def read(self, attempt_id: AgentAttemptId) -> CandidateTree | None:
        """The candidate this attempt captured, asked of the store and nothing else.

        Never of the checkout: what a run made outlives the checkout it was
        pinned from, and a store that could only be read while that checkout
        still stood would be keeping the work in the wrong place. A project that
        has captured nothing yet has no store, and that is the same answer --
        but something standing in the store's place that is not a directory is
        not, because "this attempt captured nothing" would turn a project's lost
        work into a fact about the attempt.
        """

        if not self._store.exists():
            return None
        if not self._store.is_dir():
            raise CandidateStoreUnavailable(
                f"the candidate store at {self._store} is not a directory, so "
                "nothing this project kept can be read from it"
            )
        standing = self._standing(CANDIDATE_REF_PREFIX + attempt_id.value)
        return None if standing is None else CandidateTree(attempt_id, standing)

    def _ensure_store(self) -> None:
        """Make the store exist and be one this project's objects can live in."""

        kept_in = self._checkout_object_format()
        if not self._store.exists():
            self._create_store(kept_in)
        self._refuse_unless_kept_in(kept_in)

    def _checkout_object_format(self) -> GitObjectFormat:
        try:
            return object_format_of(str(self._project_checkout))
        except GitRefused as error:
            raise CandidateStoreUnavailable(
                f"the object format of {self._project_checkout} could not be read, "
                f"so no store can be made to hold its work: {error}"
            ) from error

    def _create_store(self, kept_in: GitObjectFormat) -> None:
        """Build the whole store beside its place, then move it in with one rename.

        Two attempts of one project can finish in the same moment, and `git init`
        is not one step: a second writer would otherwise find a repository whose
        configuration was still being written and read a format that is not yet
        the one it will have. A rename is one step, so a writer finds either
        nothing there or a finished store, never half of one.
        """

        unfinished = self._unfinished_store()
        try:
            self._answered(
                (
                    "init",
                    "--bare",
                    "--quiet",
                    NO_GIT_TEMPLATE,
                    f"--object-format={kept_in.value}",
                    str(unfinished),
                ),
                working_directory=str(self._store.parent),
                environment=isolated_git_environment(),
                failure=f"the candidate store at {self._store} could not be created",
            )
            unfinished.rename(self._store)
        except OSError as error:
            if not self._store.is_dir():
                raise CandidateStoreUnavailable(
                    f"the candidate store at {self._store} could not be put in "
                    f"place: {error}"
                ) from error
        finally:
            if unfinished.exists():
                shutil.rmtree(unfinished)

    def _unfinished_store(self) -> Path:
        try:
            return Path(
                tempfile.mkdtemp(
                    dir=self._store.parent, prefix=_UNFINISHED_STORE_PREFIX
                )
            )
        except OSError as error:
            raise CandidateStoreUnavailable(
                f"the candidate store at {self._store} could not be built beside "
                f"its place: {error}"
            ) from error

    def _refuse_unless_kept_in(self, kept_in: GitObjectFormat) -> None:
        """The store names objects the way the project does, or it can hold none.

        An object's name *is* its hash, so a store of the other format is not a
        store with a wrong setting -- it is a repository that this project's trees
        cannot be written into at all.
        """

        try:
            standing = object_format_of(str(self._store))
        except GitRefused as error:
            raise CandidateStoreUnavailable(
                f"the candidate store at {self._store} could not be read: {error}"
            ) from error
        if standing != kept_in:
            raise CandidateStoreUnavailable(
                f"the candidate store at {self._store} names its objects by "
                f"{standing.value} while {self._project_checkout} names them by "
                f"{kept_in.value}, so no tree of this project can be kept there"
            )

    def _seed(self, pin: ProjectSourcePin, staging: Path) -> None:
        """Move the pinned tree into the store, so the store stands on its own.

        Only the tree travels, never the history carrying it: a tree names
        subtrees and blobs alone, so this is everything a capture needs and the
        commits the operator's checkout holds stay where they are. Without it the
        store could not read the pin it seeds the index from, and a candidate that
        rewrote only its changed directories would name subtrees nobody has.

        The move is a pack written beside the run and indexed into the store, not
        a push: a push runs whatever `pre-push` the operator's checkout carries
        and whatever `receive-pack` the host resolves, and neither is part of the
        work a run was asked to do.

        One pin is worked on by many attempts, so the store is asked first: a
        pinned tree already rooted here is already whole, and packing a project
        again for every attempt of it would be the same megabytes over and over.
        """

        rooted_at = PINNED_TREE_REF_PREFIX + pin.tree
        if self._standing(rooted_at) == pin.tree:
            return
        self._indexed_into_store(self._packed_out_of_checkout(pin, staging))
        self._root(rooted_at, pin.tree)

    def _packed_out_of_checkout(self, pin: ProjectSourcePin, staging: Path) -> Path:
        """Everything the pinned tree names, as one pack file beside the run."""

        wanted = staging / _WANTED_OBJECTS_NAME
        wanted.write_text(f"{pin.tree}\n", encoding="utf-8")
        base = staging / _PACKED_PIN_BASE_NAME
        with wanted.open("rb") as fed:
            named = _one_line(
                self._answered(
                    ("pack-objects", "--revs", "--quiet", str(base)),
                    working_directory=str(self._project_checkout),
                    environment=isolated_git_environment(),
                    failure=(
                        f"the pinned tree {pin.tree} could not be packed out of "
                        f"{self._project_checkout}"
                    ),
                    standard_input=fed,
                )
            )
        return base.with_name(f"{base.name}-{named}.pack")

    def _indexed_into_store(self, packed: Path) -> None:
        with packed.open("rb") as fed:
            self._in_store(("index-pack", "--stdin"), standard_input=fed)

    def _root(self, reference: str, tree: str) -> None:
        """Anchor the seeded material, never moving an anchor somebody else set.

        The write is a compare-and-set against "no ref yet", like a candidate's
        own: the ref carries its tree in its own name, so a ref standing at
        anything else means this store was told something untrue, and quietly
        overwriting it would keep the lie rather than surface it. Losing the race
        to a writer of the same tree is no failure -- that writer is stating the
        same fact -- but a refusal with nothing standing there at all is, because
        the seeded objects would be unreachable and a later `git gc` would take
        them.
        """

        try:
            self._in_store(("update-ref", reference, tree, _NO_REF_YET))
        except CandidateStoreUnavailable as refusal:
            standing = self._settled(reference)
            if standing == tree:
                return
            if standing is None:
                raise
            raise CandidateStoreUnavailable(
                f"{reference} in {self._store} stands at {standing} rather than "
                f"at the tree {tree} it is named for, so this store has been told "
                "something untrue and the capture will not overwrite it"
            ) from refusal

    def _written(
        self, pin: ProjectSourcePin, lease: AgentAttemptWorkspaceLease, index: Path
    ) -> str:
        with entered_leased_directory(
            lease.working_directory, lease.device, lease.inode
        ) as (entered, descriptor):
            staging = LeasedIndex(entered, descriptor, index)
            self._staged(("read-tree", pin.tree), staging)
            self._staged(("add", "--all"), staging)
            self._refuse_nested_repositories(
                self._staged(("ls-files", "--stage", "-z"), staging), lease
            )
            return _one_line(self._staged(("write-tree",), staging))

    def _staged(self, arguments: tuple[str, ...], staging: LeasedIndex) -> bytes:
        try:
            return answered_in_lease(
                arguments, leased=staging, git_directory=str(self._store)
            )
        except GitRefused as error:
            raise CandidateStoreUnavailable(
                f"the work standing in the leased workspace could not be staged: "
                f"{error}"
            ) from error

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
        anchored -- once it has finished writing it -- and only work that differs
        is a contradiction.
        """

        reference = CANDIDATE_REF_PREFIX + candidate.attempt_id.value
        try:
            self._in_store(("update-ref", reference, candidate.tree, _NO_REF_YET))
        except CandidateStoreUnavailable as refusal:
            standing = self._settled(reference)
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

    def _settled(self, reference: str) -> str | None:
        """What this ref stands at once whoever holds its lock has finished.

        A writer refused the lock is a few milliseconds ahead of the value it
        needs, not wrong about it, so the question is asked again a few times
        before "nothing stands here" is believed.
        """

        for look in range(_LOCK_HANDOVER_LOOKS):
            if look:
                time.sleep(_LOCK_HANDOVER_PAUSE_SECONDS)
            standing = self._standing(reference)
            if standing is not None:
                return standing
        return None

    def _standing(self, reference: str) -> str | None:
        named = _one_line(
            self._in_store(("for-each-ref", "--format=%(objectname)", reference))
        )
        return named or None

    def _in_store(
        self,
        arguments: tuple[str, ...],
        standard_input: int | IO[bytes] = subprocess.DEVNULL,
    ) -> bytes:
        return self._answered(
            arguments,
            working_directory=str(self._store.parent),
            environment=isolated_git_environment(GIT_DIR=str(self._store)),
            failure=f"the candidate store at {self._store} could not be reached",
            standard_input=standard_input,
        )

    def _answered(
        self,
        arguments: tuple[str, ...],
        *,
        working_directory: str,
        environment: dict[str, str],
        failure: str,
        standard_input: int | IO[bytes] = subprocess.DEVNULL,
    ) -> bytes:
        try:
            return answered_git(
                arguments,
                working_directory=working_directory,
                environment=environment,
                standard_input=standard_input,
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
