"""Publishing a budget revision: exact bytes in, the catalog's own write, hash out.

ADR 0008 makes publication the first point where a budget is judged: a revision
validates its own content, and provider, executor, model and meter compatibility
stay with the run start that alone knows them. This use-case is that judgement --
it reads the bytes against the one owner that knows what a budget is, then asks
the store that already owns the write. It does not invent a second publication,
and it asks nothing about authentication: a credential path is not a bound.
"""

from __future__ import annotations

from dataclasses import dataclass

from atelier2.application.publish_document_revision import publish_document_revision
from atelier2.application.refusals import DurableStateCorrupt, WriteUnavailable
from atelier2.contracts.budgets_v3 import (
    BudgetRevisionRefused,
    read_budget_revision_document,
)
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.ports.published_revisions import PublishedRevisionRegistry


@dataclass(frozen=True)
class BudgetPublicationCreated:
    revision: PublishedRevision


@dataclass(frozen=True)
class BudgetPublicationExisting:
    revision: PublishedRevision


@dataclass(frozen=True)
class BudgetPublicationInvalid:
    verdict: BudgetRevisionRefused


@dataclass(frozen=True)
class BudgetPublicationCollision:
    pass


type PublishBudgetRevisionResult = (
    BudgetPublicationCreated
    | BudgetPublicationExisting
    | BudgetPublicationInvalid
    | BudgetPublicationCollision
    | WriteUnavailable
    | DurableStateCorrupt
)


def publish_budget_revision(
    document: bytes, registry: PublishedRevisionRegistry
) -> PublishBudgetRevisionResult:
    verdict = read_budget_revision_document(document)
    if isinstance(verdict, BudgetRevisionRefused):
        return BudgetPublicationInvalid(verdict)
    return publish_document_revision(
        PublishedRevision(RevisionKind.BUDGET_POLICY, document),
        registry,
        created=BudgetPublicationCreated,
        existing=BudgetPublicationExisting,
        collision=BudgetPublicationCollision,
    )
