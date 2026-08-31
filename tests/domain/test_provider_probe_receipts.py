from __future__ import annotations

import json
from dataclasses import replace

from atelier2.contracts.agents import AgentConfigurationRevisionHash
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.provider_probe_receipts import (
    MAXIMUM_PROVIDER_PROBE_RECEIPT_BYTES,
    ProviderProbeProblemCode,
    ProviderProbeReceipt,
    ProviderProbeReceiptRefusal,
    ProviderProbeReceiptRefused,
    ProviderProbeResult,
    ProviderProbeVectorId,
    read_provider_probe_receipt,
)
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.contracts.when import RecordedAt


def receipt(
    result: ProviderProbeResult = ProviderProbeResult.SUCCEEDED,
) -> ProviderProbeReceipt:
    return ProviderProbeReceipt(
        ProviderProbeVectorId("workspace-tools"),
        AgentConfigurationRevisionHash("1" * 64),
        WorkflowRevisionHash("2" * 64),
        "3" * 40,
        RecordedAt("2026-09-01T08:00:00Z"),
        RecordedAt("2026-09-02T08:00:00Z"),
        result,
        RunId("provider-canary/workspace-tools/2026-09-01"),
        Sha256Hash("4" * 64) if result is ProviderProbeResult.SUCCEEDED else None,
        (
            ProviderProbeProblemCode("provider-unavailable")
            if result is ProviderProbeResult.FAILED
            else None
        ),
    )


def test_each_probe_result_round_trips_through_its_canonical_bytes() -> None:
    for result in ProviderProbeResult:
        recorded = receipt(result)

        assert read_provider_probe_receipt(recorded.canonical_bytes()) == recorded


def test_a_probe_is_valid_from_observation_until_but_not_including_expiry() -> None:
    cases = (
        ("2026-09-01T07:59:59Z", False),
        ("2026-09-01T08:00:00Z", True),
        ("2026-09-02T07:59:59Z", True),
        ("2026-09-02T08:00:00Z", False),
    )

    for instant, expected in cases:
        assert receipt().is_valid_at(RecordedAt(instant)) is expected, instant


def canonical_document(**changes: object) -> bytes:
    document = json.loads(receipt().canonical_bytes())
    for field, value in changes.items():
        if value is _MISSING:
            del document[field]
        else:
            document[field] = value
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()


_MISSING = object()

REFUSED_DOCUMENTS = (
    (
        b"x" * (MAXIMUM_PROVIDER_PROBE_RECEIPT_BYTES + 1),
        ProviderProbeReceiptRefusal.DOCUMENT_TOO_LARGE,
    ),
    (b"\xff", ProviderProbeReceiptRefusal.DOCUMENT_NOT_UTF8),
    (b"[]", ProviderProbeReceiptRefusal.NOT_A_RECEIPT_OBJECT),
    (
        receipt().canonical_bytes() + b"\n",
        ProviderProbeReceiptRefusal.DOCUMENT_NOT_CANONICAL,
    ),
    (canonical_document(unknown="field"), ProviderProbeReceiptRefusal.UNKNOWN_FIELD),
    (
        canonical_document(vector=_MISSING),
        ProviderProbeReceiptRefusal.MISSING_FIELD,
    ),
    (
        canonical_document(result="uncertain"),
        ProviderProbeReceiptRefusal.UNKNOWN_RESULT,
    ),
    (
        canonical_document(source_commit="not-a-commit"),
        ProviderProbeReceiptRefusal.INVALID_FIELD,
    ),
    (
        canonical_document(valid_until="2026-09-01T08:00:00Z"),
        ProviderProbeReceiptRefusal.INVALID_VALIDITY_WINDOW,
    ),
)


def test_every_non_receipt_form_is_refused_by_name() -> None:
    for document, reason in REFUSED_DOCUMENTS:
        verdict = read_provider_probe_receipt(document)

        assert isinstance(verdict, ProviderProbeReceiptRefused), reason.value
        assert verdict.reason is reason


def test_a_result_carries_exactly_its_own_evidence() -> None:
    cases = (
        (ProviderProbeResult.SUCCEEDED, {"terminal_hash": None}),
        (ProviderProbeResult.FAILED, {"problem_code": None}),
        (
            ProviderProbeResult.SUCCEEDED,
            {"problem_code": ProviderProbeProblemCode("provider-unavailable")},
        ),
    )

    for result, changes in cases:
        try:
            replace(receipt(result), **changes)
        except ValueError:
            continue
        raise AssertionError(f"{result.value} admitted evidence {changes}")
