"""What a reader is told about published workflow revisions.

The durable adapter builds these, the use cases carry them and the API
projects them, so they are shared values rather than the seam itself: the
port next door keeps the protocol and the answers it may give.
"""

from __future__ import annotations

from dataclasses import dataclass

from atelier2.contracts.definition_sources import RevisionProvenance
from atelier2.contracts.runs import WorkflowRevision, WorkflowRevisionHash
from atelier2.contracts.workflows_v3 import AnyWorkflowDocument


@dataclass(frozen=True)
class WorkflowRevisionProjection:
    revision: WorkflowRevision
    graph: AnyWorkflowDocument


@dataclass(frozen=True)
class WorkflowRevisionPage:
    revision_hashes: tuple[WorkflowRevisionHash, ...]
    next_after: WorkflowRevisionHash | None


@dataclass(frozen=True)
class EnrichedPageBudget:
    """What one page may spend before it stops and reports where to resume.

    A page that says what its revisions are called has to read and parse their
    documents, and those are two different costs: measured against this parser
    the parse is paid per node -- 0.66 to 1.52 ms per node, holding across a 150x
    byte range -- while the read is paid per byte. Bounding one of them leaves
    the other free, so a page spends both and stops at whichever runs out first.
    """

    maximum_nodes: int
    maximum_document_bytes: int

    def __post_init__(self) -> None:
        for name, value in self.__dict__.items():
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class ListedWorkflowRevision:
    """One revision on a page, and where its bytes first entered the catalog.

    `provenance` is None for a revision no definition source delivered -- a
    document published through the catalog's own door (ADR 0007) -- which is
    the honest answer rather than an origin invented to fill the field. It sits
    here rather than on `WorkflowRevisionProjection` because a run's bound
    revision is read through that same projection and owes no reader an origin.
    """

    projection: WorkflowRevisionProjection
    provenance: RevisionProvenance | None = None


@dataclass(frozen=True)
class DescribedWorkflowRevisionPage:
    """Revisions together with the documents they were published as, parsed.

    `next_after` is set whenever further revisions follow, whether the page ended
    on its caller's limit or on its budget, so a caller resumes the same way in
    both cases and never has to learn which bound stopped it.
    """

    items: tuple[ListedWorkflowRevision, ...]
    next_after: WorkflowRevisionHash | None
