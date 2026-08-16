from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from atelier2.contracts.hashing import Sha256Hash


class RevisionKind(StrEnum):
    """The closed registry a versioned reference of a V3 document lands in.

    ADR 0006 owns the first thirteen kinds a workflow document may reference.
    ADR 0007 adds `scorecard_policy`, `selection_policy`, `admission_policy`, and
    `agent_definition` as catalog-published kinds that a workflow does not reference.
    Together they form the one closed published-kind owner.
    """

    WORKFLOW = "workflow"
    SCHEMA = "schema"
    DETERMINISTIC_OPERATION = "deterministic_operation"
    ADAPTER_OPERATION = "adapter_operation"
    CONTEXT_SOURCE = "context_source"
    READ_OPERATION = "read_operation"
    PROFILE = "profile"
    SKILL = "skill"
    TOOL = "tool"
    POLICY = "policy"
    BUDGET_POLICY = "budget_policy"
    RETRY_POLICY = "retry_policy"
    CANCELLATION_POLICY = "cancellation_policy"
    SCORECARD_POLICY = "scorecard_policy"
    SELECTION_POLICY = "selection_policy"
    ADMISSION_POLICY = "admission_policy"
    AGENT_DEFINITION = "agent_definition"


class PublishedRevisionHash(Sha256Hash):
    """The immutable product identity of one published registry revision."""


@dataclass(frozen=True)
class PublishedRevision:
    """One immutable registry publication: exact bytes, under exactly one kind.

    The kind is deliberately outside the hash, so the identity of bytes stays what
    decision 0002 made it and a workflow document keeps one hash whether it is read
    as a `WorkflowRevision` or resolved as a published revision. The kind therefore
    travels as the lookup's own argument, which is why resolution takes both.
    """

    kind: RevisionKind
    document: bytes
    revision_hash: PublishedRevisionHash = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, RevisionKind):
            raise TypeError("a published revision names its kind through the contract")
        object.__setattr__(
            self, "revision_hash", PublishedRevisionHash.of(self.document)
        )
