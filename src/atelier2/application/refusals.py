"""The refusals every use-case of this layer shares, owned in one place.

A use-case translates a port's answer into this layer's own vocabulary, so that a
caller above never has to name the port to read the outcome. Three of those
answers are the same for every use-case that has them — the store would not
answer, the store answered with something impossible, and a write could not be
attempted — and repeating them per module would be the same word under a dozen
owners.

Two of them already existed and lived in the wrong house: `WriteUnavailable` and
`DurableStateCorrupt` were declared by `publish_workflow_revision`, and
`start_published_run` imported them from that sibling. A use-case importing
another use-case's vocabulary is how a layer loses its shape; this module is the
honest owner, and the read side joins it here.

The two words about the connected tracker join for the same reason: more than
one use-case now reads the project's source -- the queue import and the start
that resolves a work-item order -- and an instance with no connection, or an
adapter refusing what the platform answered, means the same thing to every one
of them.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReadUnavailable:
    """The store could not answer this read, and a later attempt may succeed."""

    detail: str | None = None


@dataclass(frozen=True)
class ProjectSourceNotConnected:
    """The served instance binds no project with a source-connection record."""


@dataclass(frozen=True)
class SourcePayloadMalformed:
    """The tracker answered with a shape its adapter refused to read from.

    Not a retry case: the same request earns the same answer until the provider
    or the adapter changes, so the refusal names what was wrong.
    """

    detail: str


@dataclass(frozen=True)
class ProjectionTooLarge:
    """What the store holds does not fit the bound its reader was configured with.

    A refusal of its own rather than a flavour of `ReadUnavailable`: retrying is
    the answer to one and never the answer to the other. It carries no sentence —
    whoever set the bound words it.
    """


@dataclass(frozen=True)
class WriteUnavailable:
    """The write could not be attempted, and a later attempt may succeed."""

    detail: str | None = None


@dataclass(frozen=True)
class DurableStateCorrupt:
    """The store answered with a state no sequence of writes could have produced."""
