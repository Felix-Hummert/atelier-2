from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import replace

import pytest

from atelier2.contracts.adapter_operations_v3 import AdapterOperationName
from atelier2.contracts.effect_requests import (
    HeadBranch,
    ReviewedDocumentationPullRequest,
    ReviewedDocumentReplacement,
)
from atelier2.contracts.effects import (
    MAXIMUM_UNKNOWN_OUTCOME_DETAIL_CHARACTERS,
    AdapterOperationalIdentity,
    AdapterRevision,
    AuthorizedEffectExecution,
    CanonicalRequest,
    ConfirmationSource,
    EffectAbsence,
    EffectAdapterBinding,
    EffectAdapterResponseConflict,
    EffectBinding,
    EffectDestination,
    EffectHashCollision,
    EffectId,
    EffectIdentifier,
    EffectIntent,
    EffectIntentMismatch,
    EffectIntentReference,
    EffectIntentSnapshot,
    EffectIntentState,
    EffectIntentStateConflict,
    EffectIntentStateVersion,
    EffectOutcome,
    EffectReadback,
    EffectReceipt,
    EffectRequestHash,
    EffectResult,
    EffectUnknownOutcome,
    HashedPayload,
    LogicalEffectKey,
    OperatorAuthoritativeAbsence,
    OperatorFoundEffect,
    ReconcileActor,
    ReconcileCommand,
    ReconcileCommandId,
    ReconcileCommandSnapshot,
    ReconcileCommandState,
    ReconcileDetermination,
    UnknownOutcomeReason,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.runs import RunId, WorkflowRevision
from atelier2.contracts.secret_redaction import REDACTION_MARKER
from atelier2.ports.effects import (
    EffectAdapterRegistration,
    EffectAdapterRegistry,
    EffectAdapterRegistryConflict,
)

IDENTIFIER_TYPES: list[type[EffectIdentifier]] = [
    AdapterRevision,
    AdapterOperationalIdentity,
    EffectDestination,
    EffectId,
    LogicalEffectKey,
    ReconcileActor,
    ReconcileCommandId,
]

PAYLOAD_TYPES: list[type[HashedPayload]] = [CanonicalRequest, EffectResult]

PREPARED_STATE_VERSION = EffectIntentStateVersion(0)
RECONCILED_STATE_VERSION = EffectIntentStateVersion(1)


@pytest.fixture
def binding() -> EffectBinding:
    return EffectBinding(
        logical_key=LogicalEffectKey("run-1/publish-pull-request"),
        run_id=RunId("run-1"),
        workflow_revision_hash=WorkflowRevision(b"workflow-v1").revision_hash,
        adapter_revision=AdapterRevision("github-adapter-7"),
        destination=EffectDestination("github.com/FlexOr2/atelier-2"),
        adapter_operational_identity=AdapterOperationalIdentity(
            "github-installation-42"
        ),
    )


@pytest.fixture
def intent(binding: EffectBinding) -> EffectIntent:
    return EffectIntent(binding, CanonicalRequest(b'{"title":"ship the slice"}'))


def found_receipt(
    intent: EffectIntent,
    confirmation_source: ConfirmationSource = ConfirmationSource.ADAPTER_READBACK,
    reconcile_command_id: ReconcileCommandId | None = None,
) -> EffectReceipt:
    return EffectReceipt(
        intent=intent,
        effect_id=EffectId("pull-request-42"),
        result=EffectResult(b'{"number":42}'),
        confirmation_source=confirmation_source,
        reconcile_command_id=reconcile_command_id,
    )


def authoritative_absence(intent: EffectIntent) -> EffectAbsence:
    return EffectAbsence(intent.reference)


def unknown_outcome(intent: EffectIntent) -> EffectUnknownOutcome:
    return EffectUnknownOutcome(intent.reference)


def operator_found_effect() -> OperatorFoundEffect:
    return OperatorFoundEffect(
        EffectId("pull-request-42"), EffectResult(b'{"number":42}')
    )


def reconcile_command(
    intent: EffectIntent,
    determination: ReconcileDetermination,
    expected_state_version: EffectIntentStateVersion = PREPARED_STATE_VERSION,
    evidence: str = "the destination lists pull request 42 for this logical key",
) -> ReconcileCommand:
    return ReconcileCommand(
        command_id=ReconcileCommandId("reconcile-1"),
        intent_reference=intent.reference,
        expected_intent_state_version=expected_state_version,
        actor=ReconcileActor("operator-on-call"),
        evidence=evidence,
        determination=determination,
    )


def authorized_execution(intent: EffectIntent) -> AuthorizedEffectExecution:
    command = reconcile_command(intent, OperatorAuthoritativeAbsence())

    authorization = intent.resolve_reconciliation(command, PREPARED_STATE_VERSION)

    assert isinstance(authorization, AuthorizedEffectExecution)
    return authorization


READBACKS: dict[str, Callable[[EffectIntent], EffectReadback]] = {
    "found": found_receipt,
    "authoritative-absence": authoritative_absence,
    "unknown": unknown_outcome,
}

RECEIPT_PROVENANCE: list[tuple[ConfirmationSource, ReconcileCommandId | None]] = [
    (ConfirmationSource.ADAPTER_READBACK, None),
    (ConfirmationSource.ADAPTER_EXECUTION, None),
    (ConfirmationSource.OPERATOR_FOUND, ReconcileCommandId("reconcile-1")),
    (
        ConfirmationSource.OPERATOR_AUTHORIZED_EXECUTION,
        ReconcileCommandId("reconcile-1"),
    ),
]

CONTRADICTED_RECEIPT_PROVENANCE: list[
    tuple[ConfirmationSource, ReconcileCommandId | None]
] = [
    (source, None if reconcile_command_id else ReconcileCommandId("reconcile-1"))
    for source, reconcile_command_id in RECEIPT_PROVENANCE
]

BOUND_VALUE_CHANGES: dict[str, Callable[[EffectIntent], EffectIntent]] = {
    "request-bytes": lambda intent: replace(
        intent, request=CanonicalRequest(b'{"title":"ship something else"}')
    ),
    "logical-key": lambda intent: replace(
        intent,
        binding=replace(intent.binding, logical_key=LogicalEffectKey("run-1/other")),
    ),
    "run-id": lambda intent: replace(
        intent, binding=replace(intent.binding, run_id=RunId("run-2"))
    ),
    "workflow-revision": lambda intent: replace(
        intent,
        binding=replace(
            intent.binding,
            workflow_revision_hash=WorkflowRevision(b"workflow-v2").revision_hash,
        ),
    ),
    "adapter-revision": lambda intent: replace(
        intent,
        binding=replace(
            intent.binding, adapter_revision=AdapterRevision("github-adapter-8")
        ),
    ),
    "destination": lambda intent: replace(
        intent,
        binding=replace(
            intent.binding, destination=EffectDestination("gitlab.com/FlexOr2/other")
        ),
    ),
    "adapter-operational-identity": lambda intent: replace(
        intent,
        binding=replace(
            intent.binding,
            adapter_operational_identity=AdapterOperationalIdentity(
                "github-installation-99"
            ),
        ),
    ),
}


@pytest.mark.parametrize("payload_type", PAYLOAD_TYPES)
@pytest.mark.parametrize("payload", [b"request", b"\x00\xffbytes"])
def test_hashed_payload_keeps_the_exact_bytes_and_their_sha256(
    payload_type: type[HashedPayload], payload: bytes
) -> None:
    record = payload_type(payload)

    assert record.payload == payload
    assert record.payload_hash.value == hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize("payload_type", PAYLOAD_TYPES)
def test_durable_payload_is_accepted_when_its_claimed_hash_matches(
    payload_type: type[HashedPayload],
) -> None:
    claimed_hash = payload_type.payload_hash_type.of(b"request")

    record = payload_type.from_durable_record(b"request", claimed_hash)

    assert record.payload == b"request"
    assert record.payload_hash == claimed_hash


@pytest.mark.parametrize("payload_type", PAYLOAD_TYPES)
def test_durable_payload_is_rejected_when_its_claimed_hash_contradicts_its_bytes(
    payload_type: type[HashedPayload],
) -> None:
    contradicting_hash = payload_type.payload_hash_type.of(b"other-request")

    with pytest.raises(EffectHashCollision):
        payload_type.from_durable_record(b"request", contradicting_hash)


def test_canonical_request_is_hydrated_by_the_request_hash_its_reference_names(
    intent: EffectIntent,
) -> None:
    hydrated = CanonicalRequest.from_durable_record(
        intent.request.payload, intent.reference.request_hash
    )

    assert hydrated == intent.request
    assert hydrated.request_hash == intent.reference.request_hash


def test_canonical_request_rejects_bytes_that_record_no_request() -> None:
    with pytest.raises(ValueError, match="exact request bytes"):
        CanonicalRequest(b"")


def test_effect_result_keeps_an_empty_answer_from_the_destination() -> None:
    assert EffectResult(b"").payload_hash == Sha256Hash.of(b"")


@pytest.mark.parametrize("identifier_type", IDENTIFIER_TYPES)
@pytest.mark.parametrize("value", ["effect-1", " padded ", "\N{SNOWMAN}"])
def test_effect_identifier_preserves_the_exact_caller_string(
    identifier_type: type[EffectIdentifier], value: str
) -> None:
    assert identifier_type(value).value == value


@pytest.mark.parametrize("identifier_type", IDENTIFIER_TYPES)
def test_effect_identifier_rejects_empty_input(
    identifier_type: type[EffectIdentifier],
) -> None:
    with pytest.raises(ValueError, match="nonempty"):
        identifier_type("")


@pytest.mark.parametrize("advance_count", [0, 1, 7])
def test_intent_state_version_keeps_any_nonnegative_advance_count(
    advance_count: int,
) -> None:
    assert EffectIntentStateVersion(advance_count).value == advance_count


def test_intent_state_version_rejects_a_negative_advance_count() -> None:
    with pytest.raises(ValueError, match="nonnegative"):
        EffectIntentStateVersion(-1)


@pytest.mark.parametrize("state", list(EffectIntentState))
def test_intent_snapshot_is_a_typed_point_in_time_durable_read(
    intent: EffectIntent, state: EffectIntentState
) -> None:
    snapshot = EffectIntentSnapshot(intent, state, RECONCILED_STATE_VERSION)

    assert snapshot.intent is intent
    assert snapshot.state is state
    assert snapshot.state_version == RECONCILED_STATE_VERSION


@pytest.mark.parametrize("state", list(ReconcileCommandState))
def test_reconcile_command_snapshot_is_a_typed_point_in_time_durable_read(
    intent: EffectIntent, state: ReconcileCommandState
) -> None:
    command = reconcile_command(intent, operator_found_effect())

    assert ReconcileCommandSnapshot(command, state) == ReconcileCommandSnapshot(
        command=command, state=state
    )


def test_intent_reference_fingerprints_the_exact_prepared_request(
    intent: EffectIntent,
) -> None:
    assert intent.request.request_hash == EffectRequestHash.of(intent.request.payload)
    assert intent.reference == EffectIntentReference(
        intent.binding, EffectRequestHash.of(intent.request.payload)
    )


def test_retry_is_authorized_by_the_exact_prepared_request(
    intent: EffectIntent,
) -> None:
    intent.authorize_retry(
        EffectIntent(intent.binding, CanonicalRequest(intent.request.payload))
    )


@pytest.mark.parametrize(
    "change", BOUND_VALUE_CHANGES.values(), ids=list(BOUND_VALUE_CHANGES)
)
def test_retry_is_rejected_when_a_bound_value_changed(
    intent: EffectIntent, change: Callable[[EffectIntent], EffectIntent]
) -> None:
    with pytest.raises(EffectIntentMismatch):
        intent.authorize_retry(change(intent))


def test_found_receipt_carries_the_whole_prepared_intent_and_its_result(
    intent: EffectIntent,
) -> None:
    receipt = found_receipt(intent)

    assert receipt.outcome is EffectOutcome.FOUND
    assert receipt.intent == intent
    assert receipt.intent.request.payload == intent.request.payload
    assert receipt.intent.request.payload_hash == EffectRequestHash.of(
        intent.request.payload
    )
    assert receipt.intent.binding == intent.binding
    assert receipt.effect_id == EffectId("pull-request-42")
    assert receipt.result.payload_hash == Sha256Hash.of(b'{"number":42}')


@pytest.mark.parametrize("build_readback", READBACKS.values(), ids=list(READBACKS))
def test_readback_presenting_the_prepared_intent_is_authorized(
    intent: EffectIntent, build_readback: Callable[[EffectIntent], EffectReadback]
) -> None:
    intent.authorize_readback(build_readback(intent))


@pytest.mark.parametrize("build_readback", READBACKS.values(), ids=list(READBACKS))
@pytest.mark.parametrize(
    "change", BOUND_VALUE_CHANGES.values(), ids=list(BOUND_VALUE_CHANGES)
)
def test_readback_presenting_another_prepared_value_is_rejected(
    intent: EffectIntent,
    build_readback: Callable[[EffectIntent], EffectReadback],
    change: Callable[[EffectIntent], EffectIntent],
) -> None:
    with pytest.raises(EffectIntentMismatch):
        intent.authorize_readback(build_readback(change(intent)))


def test_determination_claiming_an_unprepared_request_is_rejected(
    intent: EffectIntent,
) -> None:
    forged = EffectUnknownOutcome(
        EffectIntentReference(
            intent.binding, EffectRequestHash.of(b'{"title":"never prepared"}')
        )
    )

    with pytest.raises(EffectIntentMismatch):
        intent.authorize_readback(forged)


@pytest.mark.parametrize(
    ("build_readback", "outcome"),
    [
        (found_receipt, EffectOutcome.FOUND),
        (authoritative_absence, EffectOutcome.AUTHORITATIVE_NOT_FOUND),
        (unknown_outcome, EffectOutcome.UNKNOWN),
    ],
    ids=list(READBACKS),
)
def test_readback_reports_its_disjoint_outcome(
    intent: EffectIntent,
    build_readback: Callable[[EffectIntent], EffectReadback],
    outcome: EffectOutcome,
) -> None:
    assert build_readback(intent).outcome is outcome


def test_authoritative_absence_is_established_only_by_an_adapter_readback(
    intent: EffectIntent,
) -> None:
    absence = authoritative_absence(intent)

    assert absence.confirmation_source is ConfirmationSource.ADAPTER_READBACK


@pytest.mark.parametrize(
    "source",
    [
        ConfirmationSource.OPERATOR_FOUND,
        ConfirmationSource.OPERATOR_AUTHORIZED_EXECUTION,
    ],
)
def test_adapter_readback_cannot_spoof_operator_provenance(
    intent: EffectIntent, source: ConfirmationSource
) -> None:
    response = found_receipt(intent, source, ReconcileCommandId("forged-command"))

    with pytest.raises(EffectAdapterResponseConflict):
        intent.authorize_adapter_readback(response)


@pytest.mark.parametrize(
    ("confirmation_source", "reconcile_command_id"), RECEIPT_PROVENANCE
)
def test_receipt_records_the_source_that_established_the_effect(
    intent: EffectIntent,
    confirmation_source: ConfirmationSource,
    reconcile_command_id: ReconcileCommandId | None,
) -> None:
    receipt = found_receipt(intent, confirmation_source, reconcile_command_id)

    intent.authorize_readback(receipt)
    assert receipt.confirmation_source is confirmation_source
    assert receipt.reconcile_command_id == reconcile_command_id


@pytest.mark.parametrize(
    ("confirmation_source", "reconcile_command_id"), CONTRADICTED_RECEIPT_PROVENANCE
)
def test_receipt_rejects_a_reconcile_command_contradicting_its_source(
    intent: EffectIntent,
    confirmation_source: ConfirmationSource,
    reconcile_command_id: ReconcileCommandId | None,
) -> None:
    with pytest.raises(ValueError, match="reconcile command"):
        found_receipt(intent, confirmation_source, reconcile_command_id)


class _RegistryAdapter:
    def __init__(self, identity: str) -> None:
        self.identity = identity

    def readback(self, intent: EffectIntent) -> EffectReadback:
        return EffectUnknownOutcome(intent.reference)

    def execute(self, intent: EffectIntent) -> EffectUnknownOutcome:
        return EffectUnknownOutcome(intent.reference)

    def close(self) -> None:
        pass


class _RegistryFactory:
    def __init__(self, operation: AdapterOperationName, identity: str) -> None:
        self.identity = identity
        self.binding = EffectAdapterBinding(
            AdapterRevision("adapter-v1"),
            EffectDestination("platform"),
            AdapterOperationalIdentity(identity),
            operation,
        )
        self.proves_absence = True

    def open(self) -> _RegistryAdapter:
        return _RegistryAdapter(self.identity)


def test_effect_registry_selects_one_open_adapter_by_its_typed_operation() -> None:
    open_pr = _RegistryFactory(AdapterOperationName.OPEN_PR, "open-pr")
    push = _RegistryFactory(AdapterOperationName.PUSH_ATELIER_COMMIT, "push")
    registry = EffectAdapterRegistry(
        (
            EffectAdapterRegistration(AdapterOperationName.OPEN_PR, open_pr),
            EffectAdapterRegistration(AdapterOperationName.PUSH_ATELIER_COMMIT, push),
        )
    )

    opened = registry.open()
    try:
        selected_open_pr = opened.adapter_for(
            AdapterOperationName.OPEN_PR, open_pr.binding
        )
        selected_push = opened.adapter_for(
            AdapterOperationName.PUSH_ATELIER_COMMIT, push.binding
        )
        assert isinstance(selected_open_pr, _RegistryAdapter)
        assert selected_open_pr.identity == "open-pr"
        assert isinstance(selected_push, _RegistryAdapter)
        assert selected_push.identity == "push"
    finally:
        opened.close()


@pytest.mark.parametrize("duplicate", ["operation", "binding", "factory"])
def test_effect_registry_refuses_every_ambiguous_registration(duplicate: str) -> None:
    open_pr = _RegistryFactory(AdapterOperationName.OPEN_PR, "open-pr")
    push = _RegistryFactory(AdapterOperationName.PUSH_ATELIER_COMMIT, "push")
    if duplicate == "operation":
        registrations = (
            EffectAdapterRegistration(AdapterOperationName.OPEN_PR, open_pr),
            EffectAdapterRegistration(
                AdapterOperationName.OPEN_PR,
                _RegistryFactory(AdapterOperationName.OPEN_PR, "other"),
            ),
        )
    elif duplicate == "binding":
        push.binding = replace(
            open_pr.binding, operation_name=AdapterOperationName.PUSH_ATELIER_COMMIT
        )
        registrations = (
            EffectAdapterRegistration(AdapterOperationName.OPEN_PR, open_pr),
            EffectAdapterRegistration(AdapterOperationName.PUSH_ATELIER_COMMIT, push),
        )
    else:
        registrations = (
            EffectAdapterRegistration(AdapterOperationName.OPEN_PR, open_pr),
            EffectAdapterRegistration(
                AdapterOperationName.PUSH_ATELIER_COMMIT, open_pr
            ),
        )

    with pytest.raises(EffectAdapterRegistryConflict):
        EffectAdapterRegistry(registrations)


def test_effect_registry_refuses_a_registration_mismatched_to_its_factory() -> None:
    factory = _RegistryFactory(AdapterOperationName.OPEN_PR, "open-pr")

    with pytest.raises(EffectAdapterRegistryConflict, match="operation"):
        EffectAdapterRegistry(
            (
                EffectAdapterRegistration(
                    AdapterOperationName.PUSH_ATELIER_COMMIT, factory
                ),
            )
        )


def test_operator_found_command_resolves_to_a_receipt_for_the_prepared_intent(
    intent: EffectIntent,
) -> None:
    command = reconcile_command(intent, operator_found_effect())

    receipt = intent.resolve_reconciliation(command, PREPARED_STATE_VERSION)

    assert isinstance(receipt, EffectReceipt)
    intent.authorize_readback(receipt)
    assert receipt == found_receipt(
        intent,
        confirmation_source=ConfirmationSource.OPERATOR_FOUND,
        reconcile_command_id=command.command_id,
    )


def test_operator_absence_command_authorizes_executing_the_same_prepared_request(
    intent: EffectIntent,
) -> None:
    authorization = authorized_execution(intent)

    assert authorization == AuthorizedEffectExecution(
        intent=intent, reconcile_command_id=ReconcileCommandId("reconcile-1")
    )
    assert authorization.intent.binding.logical_key == intent.binding.logical_key
    assert authorization.intent.request.payload == intent.request.payload


def test_execution_under_operator_authorization_becomes_a_receipt_naming_its_command(
    intent: EffectIntent,
) -> None:
    authorization = authorized_execution(intent)

    receipt = authorization.confirm_execution(
        EffectId("pull-request-42"), EffectResult(b'{"number":42}')
    )

    intent.authorize_readback(receipt)
    assert receipt == found_receipt(
        intent,
        confirmation_source=ConfirmationSource.OPERATOR_AUTHORIZED_EXECUTION,
        reconcile_command_id=authorization.reconcile_command_id,
    )


def test_authoritative_absence_resolves_to_an_authorization_naming_its_command(
    intent: EffectIntent,
) -> None:
    command = reconcile_command(intent, OperatorAuthoritativeAbsence())

    assert intent.resolve_reconciliation(
        command, PREPARED_STATE_VERSION
    ) == AuthorizedEffectExecution(
        intent=intent, reconcile_command_id=command.command_id
    )


@pytest.mark.parametrize(
    "change", BOUND_VALUE_CHANGES.values(), ids=list(BOUND_VALUE_CHANGES)
)
def test_reconcile_command_naming_another_prepared_request_is_rejected(
    intent: EffectIntent, change: Callable[[EffectIntent], EffectIntent]
) -> None:
    command = reconcile_command(change(intent), OperatorAuthoritativeAbsence())

    with pytest.raises(EffectIntentMismatch):
        intent.resolve_reconciliation(command, PREPARED_STATE_VERSION)


@pytest.mark.parametrize(
    "state_version", [PREPARED_STATE_VERSION, RECONCILED_STATE_VERSION]
)
def test_reconcile_command_resolves_the_intent_state_it_was_prepared_for(
    intent: EffectIntent, state_version: EffectIntentStateVersion
) -> None:
    command = reconcile_command(
        intent, operator_found_effect(), expected_state_version=state_version
    )

    assert intent.resolve_reconciliation(command, state_version) == found_receipt(
        intent,
        confirmation_source=ConfirmationSource.OPERATOR_FOUND,
        reconcile_command_id=command.command_id,
    )


@pytest.mark.parametrize(
    ("expected_state_version", "current_state_version"),
    [
        (PREPARED_STATE_VERSION, RECONCILED_STATE_VERSION),
        (RECONCILED_STATE_VERSION, PREPARED_STATE_VERSION),
    ],
    ids=["superseded-command", "unreached-state"],
)
def test_reconcile_command_for_another_state_of_the_same_request_is_rejected(
    intent: EffectIntent,
    expected_state_version: EffectIntentStateVersion,
    current_state_version: EffectIntentStateVersion,
) -> None:
    command = reconcile_command(
        intent, operator_found_effect(), expected_state_version=expected_state_version
    )

    assert command.intent_reference == intent.reference
    with pytest.raises(EffectIntentStateConflict):
        intent.resolve_reconciliation(command, current_state_version)


def test_reconcile_command_rejects_a_determination_without_evidence(
    intent: EffectIntent,
) -> None:
    with pytest.raises(ValueError, match="evidence"):
        reconcile_command(intent, OperatorAuthoritativeAbsence(), evidence="")


def test_prepared_effect_and_reconcile_state_cannot_be_rebound(
    intent: EffectIntent,
) -> None:
    command = reconcile_command(intent, OperatorAuthoritativeAbsence())
    rebindings: list[tuple[object, str, object]] = [
        (intent, "request", CanonicalRequest(b"replaced")),
        (intent.binding, "logical_key", LogicalEffectKey("replaced")),
        (command, "determination", operator_found_effect()),
        (command, "expected_intent_state_version", RECONCILED_STATE_VERSION),
        (
            authoritative_absence(intent),
            "confirmation_source",
            ConfirmationSource.OPERATOR_FOUND,
        ),
        (
            authorized_execution(intent),
            "intent",
            replace(intent, request=CanonicalRequest(b"replaced")),
        ),
    ]

    for target, attribute, value in rebindings:
        with pytest.raises((AttributeError, TypeError)):
            setattr(target, attribute, value)


def test_an_unknown_outcome_keeps_the_last_scrubbed_words_a_destination_said() -> None:
    echoed_credential = "ghp_" + "s" * 36
    said = "noise\n" * 2_000 + f"fatal: authentication failed: {echoed_credential}"

    reason = UnknownOutcomeReason(128, 12, said)

    assert len(reason.detail) <= MAXIMUM_UNKNOWN_OUTCOME_DETAIL_CHARACTERS
    assert echoed_credential not in reason.detail
    assert reason.detail.endswith(f"fatal: authentication failed: {REDACTION_MARKER}")


def test_an_unknown_outcome_cannot_claim_a_negative_duration() -> None:
    with pytest.raises(ValueError, match="nonnegative duration"):
        UnknownOutcomeReason(None, -1, "")


def test_a_documentation_release_request_keeps_the_exact_reviewed_replacement() -> None:
    request = ReviewedDocumentationPullRequest(
        "a" * 40,
        "b" * 64,
        "c" * 64,
        (ReviewedDocumentReplacement("docs/PRODUCT.md", "d" * 64, b"new bytes\n"),),
        "Documentation release",
        "The independently reviewed change.",
        HeadBranch("atelier2/work-item/" + "e" * 64),
        draft=True,
    )

    assert (
        ReviewedDocumentationPullRequest.from_canonical_bytes(request.canonical_bytes())
        == request
    )
