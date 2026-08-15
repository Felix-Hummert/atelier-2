from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
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
from atelier2.contracts.runs import RunState

_MEMBERSHIP = re.compile(r"\b([a-z_][a-z_0-9]*)\s+(?:NOT\s+)?IN\s*\(([^)]*)\)")
_EQUALITY = re.compile(r"\b([a-z_][a-z_0-9]*)\s*(?:=|<>)\s*('[^']*'|\d+)")
_LITERAL = re.compile(r"'([^']*)'|(\d+)")
_LENGTH_BOUND = re.compile(
    r"length\(([a-z_0-9]+)\)\s*(?:BETWEEN\s+\d+\s+AND\s+(\d+)|<=\s*(\d+)|=\s*(\d+))"
)
_HEXADECIMAL_DIGITS = "NOT GLOB '*[^0-9a-f]*'"


def _literals(compared: str) -> frozenset[str | int]:
    found: set[str | int] = set()
    for match in _LITERAL.finditer(compared):
        text, number = match.group(1), match.group(2)
        found.add(text if text is not None else int(number))
    return frozenset(found)


def _check_conditions() -> Iterable[tuple[str, str]]:
    for table in schema.metadata.tables.values():
        for constraint in table.constraints:
            if isinstance(constraint, sa.CheckConstraint):
                yield table.name, str(constraint.sqltext)


def _schema_vocabularies() -> Mapping[str, frozenset[str | int]]:
    """Every literal the durable schema compares a column against, by column.

    A membership list spells the closed vocabulary outright and the equalities
    branch on its members, so their union is everything the schema believes a
    column may ever hold.
    """
    spelled: dict[str, set[str | int]] = {}
    for table_name, condition in _check_conditions():
        for pattern in (_MEMBERSHIP, _EQUALITY):
            for column, compared in pattern.findall(condition):
                spelled.setdefault(f"{table_name}.{column}", set()).update(
                    _literals(compared)
                )
    return {column: frozenset(literals) for column, literals in spelled.items()}


def _schema_length_bounds() -> tuple[Mapping[str, int], Mapping[str, int]]:
    """The upper length bound of every constrained column, hash columns apart."""
    hashes: dict[str, int] = {}
    fields: dict[str, int] = {}
    for table_name, condition in _check_conditions():
        for column, between, at_most, exactly in _LENGTH_BOUND.findall(condition):
            bound = int(between or at_most or exactly)
            hexadecimal = f"{column} {_HEXADECIMAL_DIGITS}" in condition
            target = hashes if hexadecimal else fields
            target[f"{table_name}.{column}"] = bound
    return hashes, fields


def _values(vocabulary: Iterable[StrEnum | IntEnum]) -> frozenset[str | int]:
    return frozenset(member.value for member in vocabulary)


SCHEMA_VOCABULARIES = _schema_vocabularies()
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
}

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


@pytest.mark.parametrize("column", sorted(OWNED_VOCABULARIES))
def test_a_persisted_vocabulary_holds_exactly_what_its_contract_owns(
    column: str,
) -> None:
    assert SCHEMA_VOCABULARIES[column] == OWNED_VOCABULARIES[column]


def test_every_vocabulary_the_schema_spells_has_an_owner_or_a_named_exemption() -> None:
    assert set(SCHEMA_VOCABULARIES) == set(OWNED_VOCABULARIES) | set(
        UNOWNED_VOCABULARIES
    )


def test_an_event_kind_added_without_its_check_drifts_from_the_schema() -> None:
    owned = OWNED_VOCABULARIES["run_events.event_kind"]

    assert SCHEMA_VOCABULARIES["run_events.event_kind"] != owned | {"AGENT_ABANDONED"}
    assert SCHEMA_VOCABULARIES["run_events.event_kind"] != owned - {"AGENT_FAILED"}


@pytest.mark.parametrize("column", sorted(HASH_LENGTH_BOUNDS))
def test_a_persisted_hash_column_is_bounded_at_the_length_of_a_real_digest(
    column: str,
) -> None:
    assert HASH_LENGTH_BOUNDS[column] == len(Sha256Hash.of(b"").value)


def test_every_field_length_bound_is_a_bound_a_contract_owns() -> None:
    assert set(FIELD_LENGTH_BOUNDS.values()) == {
        MAXIMUM_AGENT_FIELD_CHARACTERS,
        MAXIMUM_AGENT_OUTPUT_BYTES_V2,
        PROVIDER_ID_BOUND,
    }


def test_the_persisted_provider_id_bound_is_the_longest_the_contract_accepts() -> None:
    assert ProviderId("a" * PROVIDER_ID_BOUND).value == "a" * PROVIDER_ID_BOUND
    with pytest.raises(ValueError):
        ProviderId("a" * (PROVIDER_ID_BOUND + 1))
