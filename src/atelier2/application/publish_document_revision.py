"""The one place this layer reads the catalog store's answer to a publication.

Five doors publish a document revision -- schema, budget, tool grant, adapter
operation, agent definition -- and the store answers every one of them with the
same closed set of outcomes. Read five times, a new answer from the port is a
question five modules have to be asked; read here, it is asked once. That is the
whole reason this owner exists, and it is why what it owns stops where it does.

Reading the bytes stays with the door. What a schema is and what a budget is are
different questions, asked of different owners, refused by different names -- and
one door does not read bytes at all, because a reconstruction hands it the
revision. Folding those five meanings into one reader would hide them behind a
name that fits none of them.

Naming the outcome stays with the door too. A caller above matches on the word
its own door speaks, so this owner is told the three words and answers in them
rather than flattening five vocabularies into one.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import assert_never

from atelier2.application.refusals import DurableStateCorrupt, WriteUnavailable
from atelier2.contracts.revisions_v3 import PublishedRevision
from atelier2.ports.durable_runs import DurableStateCorrupt as PortDurableStateCorrupt
from atelier2.ports.durable_runs import DurableWriteUnavailable
from atelier2.ports.published_revisions import (
    PublishedRevisionCollision,
    PublishedRevisionCreated,
    PublishedRevisionExisting,
    PublishedRevisionRegistry,
)


def publish_document_revision[CreatedT, ExistingT, CollisionT](
    revision: PublishedRevision,
    registry: PublishedRevisionRegistry,
    *,
    created: Callable[[PublishedRevision], CreatedT],
    existing: Callable[[PublishedRevision], ExistingT],
    collision: Callable[[], CollisionT],
) -> CreatedT | ExistingT | CollisionT | WriteUnavailable | DurableStateCorrupt:
    """Ask the store for this revision and answer in the door's own words."""
    result = registry.publish_revision(revision)
    match result:
        case PublishedRevisionCreated(stored):
            return created(stored)
        case PublishedRevisionExisting(stored):
            return existing(stored)
        case PublishedRevisionCollision():
            return collision()
        case DurableWriteUnavailable():
            return WriteUnavailable()
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
