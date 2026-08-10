from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import ClassVar, Final, Self

from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.runs import RunId, WorkflowRevisionHash


class EffectHashCollision(RuntimeError):
    """Durable payload bytes disagree with the hash claimed for them."""


class EffectIntentMismatch(RuntimeError):
    """A retry, readback, or reconcile command does not present the prepared intent."""


class EffectIntentStateConflict(RuntimeError):
    """A reconcile command was prepared against a superseded prepared-intent state."""


class EffectRequestHash(Sha256Hash):
    """The immutable fingerprint of the exact request bytes one intent prepared."""


@dataclass(frozen=True)
class HashedPayload[PayloadHash: Sha256Hash]:
    """Exact bytes and the digest that identifies them in this payload's hash domain."""

    payload_hash_type: ClassVar[type[Sha256Hash]] = Sha256Hash

    payload: bytes
    payload_hash: PayloadHash = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "payload_hash", self.payload_hash_type.of(self.payload)
        )

    @classmethod
    def from_durable_record(cls, payload: bytes, claimed_hash: PayloadHash) -> Self:
        record = cls(payload)
        if record.payload_hash != claimed_hash:
            raise EffectHashCollision(
                "durable payload bytes disagree with their claimed hash"
            )
        return record


class CanonicalRequest(HashedPayload[EffectRequestHash]):
    """The exact bytes one effect sends to its destination."""

    payload_hash_type = EffectRequestHash

    def __post_init__(self) -> None:
        if self.payload == b"":
            raise ValueError("CanonicalRequest must carry the exact request bytes")
        super().__post_init__()

    @property
    def request_hash(self) -> EffectRequestHash:
        return self.payload_hash


class EffectResult(HashedPayload[Sha256Hash]):
    """The exact bytes a destination returned for a performed effect."""


@dataclass(frozen=True)
class EffectIdentifier:
    value: str

    def __post_init__(self) -> None:
        if self.value == "":
            raise ValueError(f"{type(self).__name__} must be a nonempty string")


class LogicalEffectKey(EffectIdentifier):
    """The identity under which a destination deduplicates one logical effect."""


class EffectDestination(EffectIdentifier):
    """The external system an effect acts on."""


class AdapterRevision(EffectIdentifier):
    """The immutable revision of the adapter that prepared an effect."""


class EffectId(EffectIdentifier):
    """The identity a destination assigned to the performed effect."""


class ReconcileCommandId(EffectIdentifier):
    """The identity of one operator reconciliation command."""


class ReconcileActor(EffectIdentifier):
    """The operator accountable for one reconciliation command."""


@dataclass(frozen=True)
class EffectIntentStateVersion:
    """How often the durable owner has advanced one prepared intent's state.

    The request fingerprint identifies which effect a command speaks about; this
    version says which state of that effect the command was prepared against, so
    a command may resolve only the exact state its actor inspected.
    """

    value: int

    def __post_init__(self) -> None:
        if self.value < 0:
            raise ValueError(
                "EffectIntentStateVersion must be a nonnegative advance count"
            )


class EffectOutcome(StrEnum):
    FOUND = "FOUND"
    AUTHORITATIVE_NOT_FOUND = "AUTHORITATIVE_NOT_FOUND"
    UNKNOWN = "UNKNOWN"


class ConfirmationSource(StrEnum):
    """Who established an outcome, and therefore what provenance it carries."""

    ADAPTER_READBACK = "ADAPTER_READBACK"
    ADAPTER_EXECUTION = "ADAPTER_EXECUTION"
    OPERATOR_FOUND = "OPERATOR_FOUND"
    OPERATOR_AUTHORIZED_EXECUTION = "OPERATOR_AUTHORIZED_EXECUTION"


_OPERATOR_CONFIRMATION_SOURCES: Final[frozenset[ConfirmationSource]] = frozenset(
    {
        ConfirmationSource.OPERATOR_FOUND,
        ConfirmationSource.OPERATOR_AUTHORIZED_EXECUTION,
    }
)


@dataclass(frozen=True)
class EffectBinding:
    logical_key: LogicalEffectKey
    run_id: RunId
    workflow_revision_hash: WorkflowRevisionHash
    adapter_revision: AdapterRevision
    destination: EffectDestination


@dataclass(frozen=True)
class EffectIntentReference:
    """Which prepared effect, and which exact request bytes, a determination names."""

    binding: EffectBinding
    request_hash: EffectRequestHash


@dataclass(frozen=True)
class EffectReceipt:
    """The prepared intent, the effect performed for it, and who confirmed that."""

    outcome: ClassVar[EffectOutcome] = EffectOutcome.FOUND

    intent: EffectIntent
    effect_id: EffectId
    result: EffectResult
    confirmation_source: ConfirmationSource
    reconcile_command_id: ReconcileCommandId | None = None

    def __post_init__(self) -> None:
        operator_confirmed = self.confirmation_source in _OPERATOR_CONFIRMATION_SOURCES
        if operator_confirmed != (self.reconcile_command_id is not None):
            raise ValueError(
                f"{self.confirmation_source} names its reconcile command if and only "
                "if an operator established or authorized the effect"
            )


@dataclass(frozen=True)
class EffectAbsence:
    """The destination itself reports it never performed the referenced request.

    Only a readback can establish this. An operator who finds nothing authorizes
    an execution instead; that authorization is not an authoritative absence.
    """

    outcome: ClassVar[EffectOutcome] = EffectOutcome.AUTHORITATIVE_NOT_FOUND
    confirmation_source: ClassVar[ConfirmationSource] = (
        ConfirmationSource.ADAPTER_READBACK
    )

    intent_reference: EffectIntentReference


@dataclass(frozen=True)
class EffectUnknownOutcome:
    """No source can yet say whether the referenced prepared request was performed."""

    outcome: ClassVar[EffectOutcome] = EffectOutcome.UNKNOWN

    intent_reference: EffectIntentReference


type EffectReadback = EffectReceipt | EffectAbsence | EffectUnknownOutcome


@dataclass(frozen=True)
class OperatorFoundEffect:
    """The operator observed the performed effect and its exact result."""

    effect_id: EffectId
    result: EffectResult


@dataclass(frozen=True)
class OperatorAuthoritativeAbsence:
    """The operator determined the destination never performed the prepared request."""


type ReconcileDetermination = OperatorFoundEffect | OperatorAuthoritativeAbsence


@dataclass(frozen=True)
class ReconcileCommand:
    """One accountable operator decision about one exact prepared request state."""

    command_id: ReconcileCommandId
    intent_reference: EffectIntentReference
    expected_intent_state_version: EffectIntentStateVersion
    actor: ReconcileActor
    evidence: str
    determination: ReconcileDetermination

    def __post_init__(self) -> None:
        if self.evidence == "":
            raise ValueError(
                "a reconcile command records the evidence its actor inspected"
            )


@dataclass(frozen=True)
class AuthorizedEffectExecution:
    """One accountable authorization to perform the prepared request once.

    An operator absence resolves nothing by itself: it permits exactly this
    intent, under its own logical key and request bytes, to be executed, and the
    effect the execution then reports is what becomes a receipt.
    """

    intent: EffectIntent
    reconcile_command_id: ReconcileCommandId

    def confirm_execution(
        self, effect_id: EffectId, result: EffectResult
    ) -> EffectReceipt:
        return EffectReceipt(
            intent=self.intent,
            effect_id=effect_id,
            result=result,
            confirmation_source=ConfirmationSource.OPERATOR_AUTHORIZED_EXECUTION,
            reconcile_command_id=self.reconcile_command_id,
        )


type ReconciliationResolution = EffectReceipt | AuthorizedEffectExecution


@dataclass(frozen=True)
class EffectIntent:
    binding: EffectBinding
    request: CanonicalRequest

    @property
    def reference(self) -> EffectIntentReference:
        return EffectIntentReference(self.binding, self.request.request_hash)

    def authorize_retry(self, attempt: EffectIntent) -> None:
        if attempt != self:
            raise EffectIntentMismatch(
                "a retry must present the prepared request bytes, hash, and binding"
            )

    def authorize_readback(self, readback: EffectReadback) -> None:
        if isinstance(readback, EffectReceipt):
            if readback.intent != self:
                raise EffectIntentMismatch(
                    "a receipt must present the prepared binding, bytes, and hash"
                )
            return
        if readback.intent_reference != self.reference:
            raise EffectIntentMismatch(
                "a determination must present the prepared intent and its request"
            )

    def resolve_reconciliation(
        self,
        command: ReconcileCommand,
        current_state_version: EffectIntentStateVersion,
    ) -> ReconciliationResolution:
        if command.intent_reference != self.reference:
            raise EffectIntentMismatch(
                "a reconcile command must name the prepared intent and its request"
            )
        if command.expected_intent_state_version != current_state_version:
            raise EffectIntentStateConflict(
                "a reconcile command resolves only the intent state it was prepared for"
            )
        determination = command.determination
        if isinstance(determination, OperatorFoundEffect):
            return EffectReceipt(
                intent=self,
                effect_id=determination.effect_id,
                result=determination.result,
                confirmation_source=ConfirmationSource.OPERATOR_FOUND,
                reconcile_command_id=command.command_id,
            )
        return AuthorizedEffectExecution(
            intent=self, reconcile_command_id=command.command_id
        )
