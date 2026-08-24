from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from atelier2.contracts.agents import AgentBindingSet
from atelier2.contracts.executions import SubmitWaitAnswerRequest, WaitAnswerSnapshot
from atelier2.contracts.node_records_v3 import RunInput
from atelier2.contracts.orders import (
    ArtifactOrderValue,
    AuthoredOrderValue,
    InlineOrderValue,
)
from atelier2.contracts.run_bindings import AnyRun
from atelier2.contracts.runs import RunId, WorkflowRevisionHash


@dataclass(frozen=True)
class DurableWriteUnavailable:
    pass


@dataclass(frozen=True)
class DurableStateCorrupt:
    pass


@dataclass(frozen=True)
class DurableRunCreated:
    run: AnyRun


@dataclass(frozen=True)
class DurableRunExisting:
    run: AnyRun


@dataclass(frozen=True)
class DurableRunRevisionMissing:
    pass


@dataclass(frozen=True)
class DurableRunIdentityConflict:
    pass


@dataclass(frozen=True)
class DurableRunFormatNotExecutable:
    """The named revision is published, and no runtime here executes its format."""


@dataclass(frozen=True)
class DurableInvalidAgentBindings:
    pass


@dataclass(frozen=True)
class DurableAgentConfigurationRevisionMissing:
    pass


@dataclass(frozen=True)
class DurableAgentExecutorBindingUnavailable:
    pass


@dataclass(frozen=True)
class DurableAgentExecutorCapabilityUnavailable:
    pass


@dataclass(frozen=True)
class DurableBindingConstraintRefused:
    """The two nodes named by a `distinct_from` resolved to the same occupation."""

    node: str
    distinct_from: str


@dataclass(frozen=True)
class DurableAgentPlatformEffectUnreconcilable:
    """This deployment's effect adapter cannot safely redeem an agent `open-pr` grant.

    The named agent node carries an effect-shaped `open-pr` grant, redeemed
    against the effect adapter after the attempt already succeeded. The composed
    adapter cannot prove absence (a live-GitHub search is eventually consistent),
    and an agent redemption has no Action-only `WAITING_RECONCILIATION` resting
    place, so the run is refused at admission rather than left to raise after it
    reported success (the `#430`/`#431` reconciliation precondition). The Action
    node path, which does have `WAITING_RECONCILIATION`, is unaffected.
    """

    node: str


type DurablePublishedRunResult = (
    DurableRunCreated
    | DurableRunExisting
    | DurableRunRevisionMissing
    | DurableRunIdentityConflict
    | DurableRunFormatNotExecutable
    | DurableInvalidAgentBindings
    | DurableAgentConfigurationRevisionMissing
    | DurableAgentExecutorBindingUnavailable
    | DurableAgentExecutorCapabilityUnavailable
    | DurableBindingConstraintRefused
    | DurableAgentPlatformEffectUnreconcilable
    | DurableWriteUnavailable
    | DurableStateCorrupt
    | DurableV3StartInputRefused
)


@dataclass(frozen=True)
class StartPublishedRunRequest:
    run_id: RunId
    revision_hash: WorkflowRevisionHash


@dataclass(frozen=True)
class StartPublishedRunRequestV2:
    run_id: RunId
    revision_hash: WorkflowRevisionHash
    agent_bindings: AgentBindingSet


class V3InputRefusal(StrEnum):
    """Every named way one order stops a start before anything is written."""

    MISSING = "missing"
    UNDECLARED = "undeclared"
    DUPLICATED = "duplicated"
    SCHEMA_MISMATCH = "schema-mismatch"
    VALUE_REFUSED = "value-refused"
    UNKNOWN_ARTIFACT = "unknown-artifact"


@dataclass(frozen=True)
class DurableV3StartInputRefused:
    """One order the start cannot honour, named by the input it is about.

    The name comes first because it is what an operator fixes: a refusal that
    said only "schema violated" would send them to read the document to find out
    which order they got wrong.
    """

    name: str
    refusal: V3InputRefusal
    detail: str | None = None


@dataclass(frozen=True)
class AuthoredOrder:
    """An order as a caller supplies it: a name and where its value comes from.

    The document pins the schema. A caller that also named a schema would
    be repeating a decision they do not own; the start looks the pin up.

    The value is inline bytes or the address of a published artifact, and the
    start resolves the second into the first before anything reads it.
    """

    name: str
    value: AuthoredOrderValue

    def __post_init__(self) -> None:
        if self.name == "":
            raise ValueError("an order names a nonempty input")
        if not isinstance(self.value, (InlineOrderValue, ArtifactOrderValue)):
            raise TypeError("an order names where its value comes from")


@dataclass(frozen=True)
class StartPublishedRunRequestV3:
    """A start that carries the orders the document declares, beside it.

    It is a third shape rather than a field on the V2 one because an order only
    exists for a V3 document, and a request that could carry one for a V1 or V2
    run would describe something no graph can read. A V3 document that declares
    no `graph_inputs` still starts through the V2 shape; what refuses a missing
    order is the order itself being absent, not the shape of the request.

    `run_inputs` is the durable form (name, pinned schema, bytes) the first
    door already speaks. `orders` is what a caller can honestly supply: a
    name and the exact bytes. The start pins the schema the document named.
    One start uses one of the two, never both.
    """

    run_id: RunId
    revision_hash: WorkflowRevisionHash
    agent_bindings: AgentBindingSet
    run_inputs: tuple[RunInput, ...] = ()
    orders: tuple[AuthoredOrder, ...] = ()


type AnyStartPublishedRunRequest = (
    StartPublishedRunRequest | StartPublishedRunRequestV2 | StartPublishedRunRequestV3
)


class DurablePublishedRunStarter(Protocol):
    def start_published(
        self, request: AnyStartPublishedRunRequest
    ) -> DurablePublishedRunResult: ...


@dataclass(frozen=True)
class DurableAnswerCreated:
    snapshot: WaitAnswerSnapshot


@dataclass(frozen=True)
class DurableAnswerExisting:
    snapshot: WaitAnswerSnapshot


@dataclass(frozen=True)
class DurableAnswerRunMissing:
    pass


@dataclass(frozen=True)
class DurableAnswerNodeMissing:
    pass


@dataclass(frozen=True)
class DurableAnswerRevisionConflict:
    pass


@dataclass(frozen=True)
class DurableAnswerStateConflict:
    pass


@dataclass(frozen=True)
class DurableAnswerBytesConflict:
    pass


@dataclass(frozen=True)
class DurableAnswerNotAdmitted:
    """The waiting node does not accept these bytes as an answer at all.

    Separate from the state conflict, because the two say different things to
    whoever asked: a state conflict means the run was not waiting for this, and
    this means the run *is* waiting and the value is not one this node's own
    declaration admits. Which declaration refused, and in whose words, stays
    inside the store; what travels is that the submission was never answerable.
    """

    detail: str


type DurableAnswerResult = (
    DurableAnswerCreated
    | DurableAnswerExisting
    | DurableAnswerRunMissing
    | DurableAnswerNodeMissing
    | DurableAnswerRevisionConflict
    | DurableAnswerStateConflict
    | DurableAnswerBytesConflict
    | DurableAnswerNotAdmitted
    | DurableWriteUnavailable
    | DurableStateCorrupt
)


class TransactionalWaitAnswerer(Protocol):
    def submit_result(
        self, request: SubmitWaitAnswerRequest
    ) -> DurableAnswerResult: ...
