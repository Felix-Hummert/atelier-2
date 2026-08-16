from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Mapping
from contextlib import closing
from enum import IntEnum, StrEnum

import pytest
import sqlalchemy as sa

from atelier2.adapters.dbos import schema
from atelier2.contracts.agent_attempts import (
    AGENT_ATTEMPT_ORDINAL,
    REPLACEMENT_AGENT_ATTEMPT_ORDINAL,
    AgentAttemptCancellationDisposition,
    AgentAttemptFailureCode,
    AgentAttemptProcessPhase,
    AgentAttemptRedriveState,
    AgentAttemptReplacement,
    AgentAttemptState,
)
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_FIELD_CHARACTERS,
    MAXIMUM_AGENT_OUTPUT_BYTES_V2,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AuthMode,
    ProviderId,
)
from atelier2.contracts.effects import (
    ConfirmationSource,
    EffectIntentState,
    EffectOutcome,
    ReconcileCommandState,
)
from atelier2.contracts.executions import RunEventKind, WaitAnswerState
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.node_records_v3 import (
    MAXIMUM_KIND_TOKEN_CHARACTERS,
    PersistedReceiptDisposition,
)
from atelier2.contracts.revisions_v3 import RevisionKind
from atelier2.contracts.runs import RunState

_DECLARATION = re.compile(r"^([a-z_][a-z_0-9]*) IN \(([^()]*)\)$")
_MEMBERSHIP = re.compile(r"\b([a-z_][a-z_0-9]*)\s+(?:NOT\s+)?IN\s*\(([^()]*)\)")
_EQUALITY = re.compile(r"\b([a-z_][a-z_0-9]*)\s*(?:=|<>)\s*('[^']*'|\d+)")
_LITERAL = re.compile(r"'([^']*)'|(\d+)")
_LENGTH_BOUND = re.compile(
    r"length\(([a-z_0-9]+)\)\s*(?:BETWEEN\s+\d+\s+AND\s+(\d+)|<=\s*(\d+)|=\s*(\d+))"
)
_HEXADECIMAL_DIGITS = "NOT GLOB '*[^0-9a-f]*'"

Conditions = tuple[tuple[str, str], ...]


def _literals(compared: str) -> frozenset[str | int]:
    found: set[str | int] = set()
    for match in _LITERAL.finditer(compared):
        text, number = match.group(1), match.group(2)
        found.add(text if text is not None else int(number))
    return frozenset(found)


def _check_conditions() -> Conditions:
    return tuple(
        (table.name, " ".join(str(constraint.sqltext).split()))
        for table in schema.metadata.tables.values()
        for constraint in table.constraints
        if isinstance(constraint, sa.CheckConstraint)
    )


def _declared_vocabularies(
    conditions: Conditions,
) -> Mapping[str, frozenset[str | int]]:
    """The closed set each column declares, read only from its own membership CHECK.

    A CHECK whose entire condition is `column IN (...)` is the schema saying what
    that column may ever hold. Every other CHECK that names the column branches
    on a part of that set, so reading those too would let a narrowed declaration
    stay green on the strength of a state-shape mention elsewhere.

    A table may spell more than one such CHECK, and SQLite requires every one of
    them, so what the database admits is their intersection, never their union.
    """

    declared: dict[str, frozenset[str | int]] = {}
    for table_name, condition in conditions:
        spelled = _DECLARATION.match(condition)
        if spelled is None:
            continue
        column, members = spelled.groups()
        qualified = f"{table_name}.{column}"
        admitted = _literals(members)
        still_admitted = declared.get(qualified)
        declared[qualified] = (
            admitted if still_admitted is None else still_admitted & admitted
        )
    return declared


def _compared_literals(conditions: Conditions) -> Mapping[str, frozenset[str | int]]:
    """Every literal a state-shape CHECK compares a column against."""

    compared: dict[str, set[str | int]] = {}
    for table_name, condition in conditions:
        if _DECLARATION.match(condition) is not None:
            continue
        for pattern in (_MEMBERSHIP, _EQUALITY):
            for column, mentioned in pattern.findall(condition):
                compared.setdefault(f"{table_name}.{column}", set()).update(
                    _literals(mentioned)
                )
    return {column: frozenset(literals) for column, literals in compared.items()}


def _conditions_with_declaration_rewritten(
    column: str, original: str, replacement: str
) -> Conditions:
    """The schema's conditions, with one column's own membership CHECK edited."""

    rewritten: list[tuple[str, str]] = []
    for table_name, condition in SCHEMA_CONDITIONS:
        declared = _DECLARATION.match(condition)
        declares_column = (
            declared is not None and f"{table_name}.{declared.group(1)}" == column
        )
        rewritten.append(
            (
                table_name,
                condition.replace(original, replacement)
                if declares_column
                else condition,
            )
        )
    return tuple(rewritten)


def _schema_length_bounds() -> tuple[Mapping[str, int], Mapping[str, int]]:
    """The upper length bound of every constrained column, hash columns apart."""
    hashes: dict[str, int] = {}
    fields: dict[str, int] = {}
    for table_name, condition in SCHEMA_CONDITIONS:
        for column, between, at_most, exactly in _LENGTH_BOUND.findall(condition):
            bound = int(between or at_most or exactly)
            hexadecimal = f"{column} {_HEXADECIMAL_DIGITS}" in condition
            target = hashes if hexadecimal else fields
            target[f"{table_name}.{column}"] = bound
    return hashes, fields


def _values(vocabulary: Iterable[StrEnum | IntEnum]) -> frozenset[str | int]:
    return frozenset(member.value for member in vocabulary)


SCHEMA_CONDITIONS = _check_conditions()
DECLARED_VOCABULARIES = _declared_vocabularies(SCHEMA_CONDITIONS)
COMPARED_LITERALS = _compared_literals(SCHEMA_CONDITIONS)
HASH_LENGTH_BOUNDS, FIELD_LENGTH_BOUNDS = _schema_length_bounds()
PROVIDER_ID_BOUND = FIELD_LENGTH_BOUNDS["auth_profile_revisions.provider_id"]

OWNED_ATTEMPT_ORDINALS: frozenset[str | int] = frozenset(
    {AGENT_ATTEMPT_ORDINAL, REPLACEMENT_AGENT_ATTEMPT_ORDINAL}
)
"""The only ordinals `AgentAttemptId.for_execution` mints an attempt for."""

OWNED_VOCABULARIES: Mapping[str, frozenset[str | int]] = {
    "agent_attempts.attempt_ordinal": OWNED_ATTEMPT_ORDINALS,
    "agent_attempts.cancellation_disposition": _values(
        AgentAttemptCancellationDisposition
    ),
    "agent_attempts.failure_code": _values(AgentAttemptFailureCode),
    "agent_attempts.process_phase": _values(AgentAttemptProcessPhase),
    "agent_attempts.redrive_state": _values(AgentAttemptRedriveState),
    "agent_attempts.replacement": _values(AgentAttemptReplacement),
    "agent_attempts.state": _values(AgentAttemptState),
    "agent_configuration_revisions.requested_capability": _values(
        AgentExecutionCapability
    ),
    "agent_configuration_revisions.revision_format_version": _values(
        AgentConfigurationRevisionFormatVersion
    ),
    "agent_receipts_v2.auth_mode": _values(AuthMode),
    "auth_profile_revisions.auth_mode": _values(AuthMode),
    "effect_intents.state": _values(EffectIntentState),
    "effect_receipts.confirmation_source": _values(ConfirmationSource),
    # An operator determination is a decision, and UNKNOWN is what a readback
    # reports when no source can decide yet, so no command may ever persist it.
    "reconcile_commands.determination": _values(EffectOutcome)
    - {EffectOutcome.UNKNOWN.value},
    "reconcile_commands.state": _values(ReconcileCommandState),
    "run_events.attempt_ordinal": OWNED_ATTEMPT_ORDINALS,
    "run_events.event_kind": _values(RunEventKind),
    "run_events.replacement": _values(AgentAttemptReplacement),
    "runs.state": _values(RunState),
    "wait_answers.state": _values(WaitAnswerState),
    "node_receipts_v3.disposition": _values(PersistedReceiptDisposition),
    "published_revisions.kind": _values(RevisionKind),
    "catalog_lineages.kind": _values(RevisionKind),
}

UNDECLARED_VOCABULARIES: frozenset[str] = frozenset(
    {
        "agent_attempts.cancellation_disposition",
        "agent_attempts.failure_code",
        "agent_attempts.redrive_state",
        "agent_attempts.replacement",
        "agent_attempts.state",
        "run_events.attempt_ordinal",
        "run_events.replacement",
    }
)
"""Owned columns the schema constrains only inside a state-shape CHECK.

For these the schema never spells the closed set in one place, so the suite can
prove that no CHECK compares them against a value their contract does not own,
but not that the schema still admits every value it does. Naming them keeps that
gap visible and makes a column that loses its own membership CHECK a red test.
"""

UNOWNED_VOCABULARIES: Mapping[str, str] = {
    "runs.workflow_format_version": (
        "the workflow format axis has no typed owner yet; #39/#47 introduce it"
    ),
    "agent_attempts.state_version": (
        "a monotonic state-machine counter, not a closed vocabulary"
    ),
    "wait_answers.state_version": (
        "a monotonic state-machine counter, not a closed vocabulary"
    ),
}

OWNED_HASH_COLUMNS: frozenset[str] = frozenset(
    {
        "agent_attempts.attempt_id",
        "agent_attempts.node_execution_id",
        "agent_attempts.request_hash",
        "agent_attempts.workflow_revision_hash",
        "agent_configuration_revisions.auth_profile_revision_hash",
        "agent_configuration_revisions.revision_hash",
        "agent_receipts.node_execution_id",
        "agent_receipts.output_hash",
        "agent_receipts.receipt_hash",
        "agent_receipts.request_hash",
        "agent_receipts.workflow_revision_hash",
        "agent_receipts_v2.agent_configuration_revision_hash",
        "agent_receipts_v2.auth_profile_revision_hash",
        "agent_receipts_v2.binding_set_hash",
        "agent_receipts_v2.node_execution_id",
        "agent_receipts_v2.output_hash",
        "agent_receipts_v2.receipt_hash",
        "agent_receipts_v2.request_hash",
        "agent_receipts_v2.workflow_revision_hash",
        "auth_profile_revisions.revision_hash",
        "effect_intents.request_hash",
        "effect_intents.workflow_revision_hash",
        "effect_receipts.request_hash",
        "effect_receipts.result_hash",
        "effect_receipts.workflow_revision_hash",
        "reconcile_commands.found_result_hash",
        "run_agent_bindings.agent_configuration_revision_hash",
        "run_agent_bindings.binding_set_hash",
        "run_agent_bindings.revision_hash",
        "run_events.agent_attempt_id",
        "run_events.event_hash",
        "run_events.node_execution_id",
        "run_events.payload_hash",
        "run_events.receipt_result_hash",
        "runs.agent_binding_set_hash",
        "runs.terminal_hash",
        "wait_answers.answer_hash",
        "wait_answers.node_execution_id",
        "workflow_revisions.revision_hash",
        "published_revisions.revision_hash",
        "catalog_lineages.lineage_id",
        "catalog_lineages.founding_revision_hash",
        "catalog_lineage_members.revision_hash",
        "node_receipts_v3.node_execution_id",
        "node_receipts_v3.request_hash",
        "node_receipts_v3.context_package_hash",
        "node_receipts_v3.receipt_hash",
    }
)

OWNED_FIELD_BOUNDS: Mapping[str, int] = {
    "agent_attempts.cancellation_command_id": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "agent_attempts.executor_operational_identity": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "agent_attempts.node_id": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "agent_attempts.process_owner_id": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "agent_attempts.watchdog_generation_id": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "agent_configuration_revisions.executor_revision": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "agent_configuration_revisions.model": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "agent_receipts_v2.executor_operational_identity": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "agent_receipts_v2.executor_revision": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "agent_receipts_v2.model": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "agent_receipts_v2.node_id": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "agent_receipts_v2.output_bytes": MAXIMUM_AGENT_OUTPUT_BYTES_V2,
    "agent_receipts_v2.profile_id": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "agent_receipts_v2.provider_id": PROVIDER_ID_BOUND,
    "agent_receipts_v2.role": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "auth_profile_revisions.profile_id": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "auth_profile_revisions.provider_id": PROVIDER_ID_BOUND,
    "run_agent_bindings.role": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "run_events.cancellation_command_id": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "catalog_lineages.kind": MAXIMUM_KIND_TOKEN_CHARACTERS,
    "published_revisions.kind": MAXIMUM_KIND_TOKEN_CHARACTERS,
}


@pytest.mark.parametrize(
    "column", sorted(set(OWNED_VOCABULARIES) - UNDECLARED_VOCABULARIES)
)
def test_a_declared_vocabulary_holds_exactly_what_its_contract_owns(
    column: str,
) -> None:
    assert DECLARED_VOCABULARIES[column] == OWNED_VOCABULARIES[column]


@pytest.mark.parametrize("column", sorted(OWNED_VOCABULARIES))
def test_no_check_compares_a_column_against_a_value_its_contract_refuses(
    column: str,
) -> None:
    assert COMPARED_LITERALS.get(column, frozenset()) <= OWNED_VOCABULARIES[column]


def test_the_columns_named_undeclared_are_exactly_the_ones_without_their_own_check() -> (
    None
):
    assert UNDECLARED_VOCABULARIES == frozenset(OWNED_VOCABULARIES) - set(
        DECLARED_VOCABULARIES
    )


def test_every_vocabulary_the_schema_spells_has_an_owner_or_a_named_exemption() -> None:
    assert set(DECLARED_VOCABULARIES) | set(COMPARED_LITERALS) == set(
        OWNED_VOCABULARIES
    ) | set(UNOWNED_VOCABULARIES)


def test_a_kind_dropped_from_its_own_check_is_drift_though_another_check_names_it() -> (
    None
):
    narrowed = _conditions_with_declaration_rewritten(
        "run_events.event_kind", "'AGENT_FAILED', ", ""
    )

    assert _declared_vocabularies(narrowed)["run_events.event_kind"] == (
        OWNED_VOCABULARIES["run_events.event_kind"] - {RunEventKind.AGENT_FAILED.value}
    )
    assert (
        RunEventKind.AGENT_FAILED.value
        in _compared_literals(narrowed)["run_events.event_kind"]
    )


def test_a_kind_added_to_its_own_check_without_a_contract_is_drift() -> None:
    widened = _conditions_with_declaration_rewritten(
        "run_events.event_kind",
        "'SUBWORKFLOW_COMPLETED'",
        "'SUBWORKFLOW_COMPLETED', 'AGENT_ABANDONED'",
    )

    assert _declared_vocabularies(widened)["run_events.event_kind"] == (
        OWNED_VOCABULARIES["run_events.event_kind"] | {"AGENT_ABANDONED"}
    )


def test_a_column_under_two_membership_checks_admits_only_their_intersection() -> None:
    """The premise a declaration is read under: SQLite requires every CHECK at once."""

    with closing(sqlite3.connect(":memory:")) as database:
        database.execute(
            "CREATE TABLE two_declarations (kind TEXT NOT NULL, "
            "CHECK (kind IN ('KEPT', 'SHUT_OUT')), CHECK (kind IN ('KEPT')))"
        )

        database.execute("INSERT INTO two_declarations VALUES ('KEPT')")
        with pytest.raises(sqlite3.IntegrityError):
            database.execute("INSERT INTO two_declarations VALUES ('SHUT_OUT')")


def test_a_second_check_narrowing_a_column_is_drift_though_the_first_names_every_kind() -> (
    None
):
    narrowed = (
        *SCHEMA_CONDITIONS,
        ("run_events", f"event_kind IN ('{RunEventKind.AGENT_COMPLETED.value}')"),
    )

    assert _declared_vocabularies(narrowed)["run_events.event_kind"] == {
        RunEventKind.AGENT_COMPLETED.value
    }


def test_every_persisted_hash_column_is_bounded_at_the_length_of_a_real_digest() -> (
    None
):
    assert set(HASH_LENGTH_BOUNDS) == OWNED_HASH_COLUMNS
    assert set(HASH_LENGTH_BOUNDS.values()) == {len(Sha256Hash.of(b"").value)}


def test_every_field_length_bound_is_the_bound_its_contract_owns() -> None:
    assert FIELD_LENGTH_BOUNDS == OWNED_FIELD_BOUNDS


def test_the_persisted_provider_id_bound_is_the_longest_the_contract_accepts() -> None:
    assert ProviderId("a" * PROVIDER_ID_BOUND).value == "a" * PROVIDER_ID_BOUND
    with pytest.raises(ValueError):
        ProviderId("a" * (PROVIDER_ID_BOUND + 1))
