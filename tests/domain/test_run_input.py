from __future__ import annotations

import pytest

from atelier2.contracts.run_inputs import (
    MAXIMUM_RUN_INPUT_BYTES,
    MAXIMUM_RUN_INPUT_MEMBERS,
    ContextPackageHash,
    RunInputContextPackage,
    RunInputMediaType,
    RunInputMember,
    RunInputName,
)


def text_member(name: str, content: bytes = b"paint the door") -> RunInputMember:
    return RunInputMember(RunInputName(name), RunInputMediaType.TEXT, content)


def test_a_member_binds_its_name_media_type_and_content_into_one_hash() -> None:
    member = text_member("order")

    assert member.member_hash != text_member("brief").member_hash
    assert member.member_hash != text_member("order", b"paint the wall").member_hash
    assert (
        member.member_hash
        != RunInputMember(
            RunInputName("order"), RunInputMediaType.BYTES, b"paint the door"
        ).member_hash
    )


def test_a_member_of_the_same_name_type_and_content_is_the_same_member() -> None:
    assert text_member("order").member_hash == text_member("order").member_hash


@pytest.mark.parametrize(
    "value",
    ["", "Order", "1order", "order-note", "order note", "ä", "o" * 65],
)
def test_a_run_input_name_refuses_anything_but_a_lowercase_slug(value: str) -> None:
    with pytest.raises(ValueError, match="run input name"):
        RunInputName(value)


@pytest.mark.parametrize("value", ["o", "order", "order_2", "o" * 64])
def test_a_run_input_name_accepts_a_lowercase_slug(value: str) -> None:
    assert RunInputName(value).value == value


def test_a_text_member_refuses_content_that_is_not_utf8() -> None:
    with pytest.raises(ValueError, match="valid UTF-8"):
        text_member("order", b"\xff\xfe")


def test_a_bytes_member_carries_content_no_text_decoder_would_accept() -> None:
    member = RunInputMember(RunInputName("order"), RunInputMediaType.BYTES, b"\xff\xfe")

    assert member.content == b"\xff\xfe"


def test_a_member_carries_a_real_document_no_authored_field_could_hold() -> None:
    document = ("a paragraph of the brief.\n" * 800).encode("utf-8")

    assert len(document) > 14_000
    assert len(text_member("order", document).content) == len(document)


def test_a_member_refuses_content_that_is_empty_or_beyond_its_byte_limit() -> None:
    with pytest.raises(ValueError, match="'order'"):
        text_member("order", b"")
    with pytest.raises(ValueError, match="'order'"):
        text_member("order", b"a" * (MAXIMUM_RUN_INPUT_BYTES + 1))


def test_a_context_package_refuses_members_that_together_exceed_its_bound() -> None:
    half = b"a" * (MAXIMUM_RUN_INPUT_BYTES // 2 + 1)

    with pytest.raises(ValueError, match="over all its members"):
        RunInputContextPackage((text_member("order", half), text_member("brief", half)))


def test_a_member_refuses_a_name_or_media_type_outside_its_typed_contract() -> None:
    with pytest.raises(TypeError, match="typed contracts"):
        RunInputMember("order", RunInputMediaType.TEXT, b"paint")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="typed contracts"):
        RunInputMember(RunInputName("order"), "text/plain", b"paint")  # type: ignore[arg-type]


def test_a_context_package_hash_is_independent_of_the_supplied_member_order() -> None:
    first = text_member("order")
    second = text_member("brief", b"one paragraph")

    assert (
        RunInputContextPackage((first, second)).context_package_hash
        == RunInputContextPackage((second, first)).context_package_hash
    )


def test_a_context_package_orders_its_members_by_name() -> None:
    package = RunInputContextPackage((text_member("order"), text_member("brief")))

    assert tuple(member.name.value for member in package.members) == ("brief", "order")


def test_a_context_package_of_different_members_is_a_different_package() -> None:
    package = RunInputContextPackage((text_member("order"),))

    assert (
        package.context_package_hash
        != RunInputContextPackage(
            (text_member("order", b"paint the wall"),)
        ).context_package_hash
    )
    assert (
        package.context_package_hash
        != RunInputContextPackage(
            (text_member("order"), text_member("brief"))
        ).context_package_hash
    )
    assert isinstance(package.context_package_hash, ContextPackageHash)


def test_a_context_package_refuses_two_members_of_the_same_name() -> None:
    with pytest.raises(ValueError, match="unique"):
        RunInputContextPackage((text_member("order"), text_member("order", b"other")))


def test_a_context_package_refuses_carrying_no_member_at_all() -> None:
    with pytest.raises(ValueError, match="1.."):
        RunInputContextPackage(())


def test_a_context_package_refuses_more_members_than_it_may_carry() -> None:
    members = tuple(
        text_member(f"member_{index}") for index in range(MAXIMUM_RUN_INPUT_MEMBERS + 1)
    )

    with pytest.raises(ValueError, match="1.."):
        RunInputContextPackage(members)


def test_a_context_package_finds_the_member_a_node_addresses_by_name() -> None:
    package = RunInputContextPackage((text_member("order"), text_member("brief")))

    assert package.member(RunInputName("order")).content == b"paint the door"
    with pytest.raises(KeyError):
        package.member(RunInputName("sketch"))
