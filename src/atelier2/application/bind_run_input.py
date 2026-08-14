"""Bind the order a run start carries to what its published workflow declares.

The decision is pure and happens before any durable run exists, so a run whose
order does not satisfy its workflow is refused by name before a provider process
can be started.
"""

from __future__ import annotations

from dataclasses import dataclass

from atelier2.contracts.run_inputs import (
    RunInputContextPackage,
    RunInputName,
    RunInputSchema,
)


@dataclass(frozen=True)
class RunInputBound:
    package: RunInputContextPackage | None


@dataclass(frozen=True)
class RunInputMembersMissing:
    names: tuple[RunInputName, ...]


@dataclass(frozen=True)
class RunInputMembersUnexpected:
    names: tuple[RunInputName, ...]


@dataclass(frozen=True)
class RunInputMemberTypeMismatch:
    names: tuple[RunInputName, ...]


type BindRunInputResult = (
    RunInputBound
    | RunInputMembersMissing
    | RunInputMembersUnexpected
    | RunInputMemberTypeMismatch
)


def _named(names: frozenset[RunInputName]) -> tuple[RunInputName, ...]:
    return tuple(sorted(names, key=lambda name: name.value.encode("ascii")))


def bind_run_input(
    schema: RunInputSchema, supplied: RunInputContextPackage | None
) -> BindRunInputResult:
    supplied_names = frozenset() if supplied is None else supplied.names
    missing = schema.names - supplied_names
    if missing:
        return RunInputMembersMissing(_named(missing))
    unexpected = supplied_names - schema.names
    if unexpected:
        return RunInputMembersUnexpected(_named(unexpected))
    if supplied is None:
        return RunInputBound(None)
    mismatched = frozenset(
        member.name
        for member in supplied.members
        if schema.media_type_of(member.name) is not member.media_type
    )
    if mismatched:
        return RunInputMemberTypeMismatch(_named(mismatched))
    return RunInputBound(supplied)
