from __future__ import annotations

import pytest

from atelier2.application.bind_run_input import (
    RunInputBound,
    RunInputMembersMissing,
    RunInputMembersUnexpected,
    RunInputMemberTypeMismatch,
    bind_run_input,
)
from atelier2.contracts.run_inputs import (
    DeclaredRunInput,
    RunInputContextPackage,
    RunInputMediaType,
    RunInputMember,
    RunInputName,
    RunInputSchema,
)

NO_DECLARED_INPUT = RunInputSchema(())
ORDER_DECLARED = RunInputSchema(
    (DeclaredRunInput(RunInputName("order"), RunInputMediaType.TEXT),)
)


def text_member(name: str, content: bytes = b"paint the door") -> RunInputMember:
    return RunInputMember(RunInputName(name), RunInputMediaType.TEXT, content)


def package(*members: RunInputMember) -> RunInputContextPackage:
    return RunInputContextPackage(members)


def test_a_declared_input_binds_to_the_supplied_package() -> None:
    supplied = package(text_member("order"))

    assert bind_run_input(ORDER_DECLARED, supplied) == RunInputBound(supplied)


def test_the_same_workflow_binds_a_different_order_without_a_new_revision() -> None:
    first = bind_run_input(ORDER_DECLARED, package(text_member("order", b"paint")))
    second = bind_run_input(ORDER_DECLARED, package(text_member("order", b"sand")))

    assert isinstance(first, RunInputBound)
    assert isinstance(second, RunInputBound)
    assert first.package is not None and second.package is not None
    assert first.package.context_package_hash != second.package.context_package_hash


def test_a_workflow_declaring_no_input_binds_a_run_that_supplies_none() -> None:
    assert bind_run_input(NO_DECLARED_INPUT, None) == RunInputBound(None)


def test_a_declared_input_that_no_run_supplies_is_refused_by_name() -> None:
    assert bind_run_input(ORDER_DECLARED, None) == RunInputMembersMissing(
        (RunInputName("order"),)
    )


def test_a_partly_supplied_package_names_every_missing_member() -> None:
    schema = RunInputSchema(
        (
            DeclaredRunInput(RunInputName("order"), RunInputMediaType.TEXT),
            DeclaredRunInput(RunInputName("brief"), RunInputMediaType.TEXT),
            DeclaredRunInput(RunInputName("sketch"), RunInputMediaType.BYTES),
        )
    )

    assert bind_run_input(schema, package(text_member("order"))) == (
        RunInputMembersMissing((RunInputName("brief"), RunInputName("sketch")))
    )


def test_a_member_the_workflow_never_declared_is_refused_by_name() -> None:
    supplied = package(text_member("order"), text_member("sketch"))

    assert bind_run_input(ORDER_DECLARED, supplied) == RunInputMembersUnexpected(
        (RunInputName("sketch"),)
    )


def test_a_workflow_declaring_no_input_refuses_any_supplied_member() -> None:
    supplied = package(text_member("order"))

    assert bind_run_input(NO_DECLARED_INPUT, supplied) == RunInputMembersUnexpected(
        (RunInputName("order"),)
    )


def test_a_member_of_another_media_type_than_declared_is_refused_by_name() -> None:
    supplied = package(
        RunInputMember(RunInputName("order"), RunInputMediaType.BYTES, b"\xff")
    )

    assert bind_run_input(ORDER_DECLARED, supplied) == RunInputMemberTypeMismatch(
        (RunInputName("order"),)
    )


def test_a_missing_member_is_named_before_an_unexpected_one() -> None:
    supplied = package(text_member("sketch"))

    assert bind_run_input(ORDER_DECLARED, supplied) == RunInputMembersMissing(
        (RunInputName("order"),)
    )


def test_a_schema_refuses_declaring_one_name_twice() -> None:
    with pytest.raises(ValueError, match="unique"):
        RunInputSchema(
            (
                DeclaredRunInput(RunInputName("order"), RunInputMediaType.TEXT),
                DeclaredRunInput(RunInputName("order"), RunInputMediaType.BYTES),
            )
        )
