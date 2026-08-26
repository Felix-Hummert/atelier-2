"""Where the work an attempt made is kept once, unchanged, past its workspace.

An attempt works in a leased directory that is deleted when it ends. Everything
the attempt produced lives only there, so between the last verified state and the
release of that directory there is exactly one moment in which the work can be
kept -- and if it is not kept then, no later owner can invent it back.

This port is that keeping. A capture answers with the tree the work now *is*:
content-addressed, so nothing can change it afterwards without changing its name,
and anchored under the attempt that made it, so a reader with nothing but the
attempt's identity can find it again. Capture is repeatable rather than
once-only: the same attempt capturing the same work twice is the same fact
stated twice, while the same attempt claiming two different trees is a
contradiction and is refused instead of overwritten.

Nothing here deletes. What an attempt made outlives the run that made it, and no
owner in this slice prunes a candidate -- a named gap, not an oversight.
"""

from __future__ import annotations

from typing import Protocol

from atelier2.contracts.agent_attempts import AgentAttemptId
from atelier2.contracts.project_sources import CandidateTree, ProjectSourcePin
from atelier2.ports.agent_executions import AgentAttemptWorkspaceLease


class CandidateStoreUnavailable(Exception):
    """The store could not answer, so nothing is claimed about the work."""


class CandidateCaptureConflict(Exception):
    """This attempt is already anchored at other work than the work offered.

    One attempt is one piece of work. Two different trees under one attempt would
    mean the store had been told two incompatible truths, and the second one is
    refused rather than allowed to overwrite the first.
    """


class CandidateTreeUnrepresentable(Exception):
    """The workspace holds something no tree of this store can carry.

    A nested repository is the case that exists: it would be recorded as a link
    to a commit the store has never seen, so the candidate would name work that
    is nowhere -- a tree that lies rather than a tree that is missing something.
    """


class CandidateTreeStore(Protocol):
    """The provider-neutral owner of every candidate one project ever captured."""

    def capture(
        self, pin: ProjectSourcePin, lease: AgentAttemptWorkspaceLease
    ) -> CandidateTree:
        """Keep what stands in this lease as the tree the pinned source became."""
        ...

    def read(self, attempt_id: AgentAttemptId) -> CandidateTree | None:
        """The candidate this attempt captured, or nothing if it captured none."""
        ...
