"""Whether exact bytes are a value one accepted schema admits.

`schemas_v3` proves a published revision *is* a schema. Nothing proved a value
against one, and the module said so about itself: resolution and, later, the
instance evaluation ask exactly one owner. Until that half exists, every sentence
that refuses a schema-violating input by name -- #38's run input above all -- has
no owner able to make it true.

These tests pin the evaluation as the profile's sibling: bounded before it is
read, refused by name, and naming its violation by a stated rule rather than by
whichever error the library happened to yield first.

Retrieval is not tested here on purpose: the profile already refuses a non-local
reference in the schema, so an accepted schema cannot reach for one, and a test
that drove the registry directly would prove the library's behaviour rather than
this owner's.
"""

from __future__ import annotations

import pytest

from atelier2.contracts.schemas_v3 import (
    MAXIMUM_INSTANCE_DOCUMENT_BYTES,
    InstanceAccepted,
    InstanceRefusal,
    InstanceRefused,
    SchemaAccepted,
    read_instance_document,
    read_schema_document,
)


def accepted(document: bytes) -> SchemaAccepted:
    verdict = read_schema_document(document)
    assert isinstance(verdict, SchemaAccepted), verdict
    return verdict


A_MEAL = accepted(
    b'{"type": "object", "properties": {"name": {"type": "string"}, '
    b'"portions": {"type": "integer", "minimum": 1}}, '
    b'"required": ["name", "portions"], "additionalProperties": false}'
)


def test_a_value_the_schema_admits_is_accepted_and_hands_back_what_it_read() -> None:
    """The caller gets the decoded value, so nothing parses these bytes twice."""
    verdict = read_instance_document(b'{"name": "lasagne", "portions": 4}', A_MEAL)

    assert isinstance(verdict, InstanceAccepted)
    assert verdict.value == {"name": "lasagne", "portions": 4}


def test_a_value_the_schema_refuses_is_named_with_the_place_it_is_about() -> None:
    """A refusal an operator cannot locate is a refusal they cannot fix."""
    verdict = read_instance_document(b'{"name": "lasagne", "portions": 0}', A_MEAL)

    assert isinstance(verdict, InstanceRefused)
    assert verdict.refusal is InstanceRefusal.SCHEMA_VIOLATED
    assert verdict.subject is not None
    assert "portions" in verdict.subject


def test_the_named_violation_is_the_earliest_place_not_whichever_arrived_first() -> (
    None
):
    """A refusal an operator cannot reproduce is not a contract.

    The evaluator promises no order over its errors, and for this very value it
    yields the deepest one first. Handing that on would mean the same bytes read
    back differently under another version of the library, so the owner names
    the violation at the earliest place instead -- here the value itself, whose
    required property is missing, not the nested `portions`.
    """
    violating = b'{"portions": 0, "surprise": true}'

    verdict = read_instance_document(violating, A_MEAL)

    assert isinstance(verdict, InstanceRefused)
    assert verdict.subject is not None
    assert verdict.subject.startswith("the value itself: ")
    assert "portions" not in verdict.subject


@pytest.mark.parametrize(
    ("label", "document", "expected"),
    (
        (
            "not utf-8",
            b'{"name": "\xff", "portions": 1}',
            InstanceRefusal.INSTANCE_NOT_UTF8,
        ),
        (
            "a byte order mark",
            "﻿{}".encode(),
            InstanceRefusal.INSTANCE_CARRIES_BYTE_ORDER_MARK,
        ),
        ("not json", b"{name: lasagne}", InstanceRefusal.INSTANCE_NOT_JSON),
        (
            "a non-canonical number",
            b'{"name": "x", "portions": NaN}',
            InstanceRefusal.NON_CANONICAL_NUMBER,
        ),
        (
            "a duplicate key",
            b'{"name": "x", "name": "y", "portions": 1}',
            InstanceRefusal.DUPLICATE_OBJECT_KEY,
        ),
    ),
    ids=("utf8", "bom", "json", "number", "duplicate"),
)
def test_bytes_that_are_no_value_at_all_are_refused_by_their_own_name(
    label: str, document: bytes, expected: InstanceRefusal
) -> None:
    """Each way of not being a value has its own name, as the schema half does."""
    verdict = read_instance_document(document, A_MEAL)

    assert isinstance(verdict, InstanceRefused)
    assert verdict.refusal is expected


def test_an_oversized_value_is_refused_before_anything_reads_it() -> None:
    """The bound exists so hostile input costs a length check, not an evaluation."""
    oversized = b'{"name": "' + b"x" * MAXIMUM_INSTANCE_DOCUMENT_BYTES + b'"}'

    verdict = read_instance_document(oversized, A_MEAL)

    assert isinstance(verdict, InstanceRefused)
    assert verdict.refusal is InstanceRefusal.INSTANCE_TOO_LARGE


@pytest.mark.parametrize(
    ("label", "document", "expected"),
    (
        (
            "deeper than the evaluation admits",
            b"[" * 40 + b"]" * 40,
            InstanceRefusal.INSTANCE_TOO_DEEP,
        ),
        (
            "more values than the evaluation admits",
            b"[" + b"1," * 5_000 + b"1]",
            InstanceRefusal.TOO_MANY_VALUES,
        ),
    ),
    ids=("depth", "values"),
)
def test_a_value_outside_the_evaluation_bounds_is_refused_by_name(
    label: str, document: bytes, expected: InstanceRefusal
) -> None:
    """The same two bounds the schema half carries, for the same reason."""
    anything = accepted(b"true")

    verdict = read_instance_document(document, anything)

    assert isinstance(verdict, InstanceRefused)
    assert verdict.refusal is expected


def test_a_recursive_schema_the_profile_admits_evaluates_to_an_answer() -> None:
    """What the profile promised is decidable must actually decide here."""
    tree = accepted(b'{"type": "object", "properties": {"child": {"$ref": "#"}}}')

    assert isinstance(
        read_instance_document(b'{"child": {"child": {}}}', tree), InstanceAccepted
    )
    assert isinstance(read_instance_document(b'{"child": 7}', tree), InstanceRefused)


@pytest.mark.parametrize(
    ("label", "schema", "instance", "admitted"),
    (
        (
            "the adjacent decimal below",
            b'{"const": 9007199254740992.0}',
            b"9007199254740992.0",
            True,
        ),
        (
            "the adjacent decimal above",
            b'{"const": 9007199254740992.0}',
            b"9007199254740993.0",
            False,
        ),
        ("an exponent past float range", b'{"type": "number"}', b"1e400", True),
        ("that exponent is not infinity", b'{"const": 1e400}', b"2e400", False),
        ("a decimal fraction keeps its identity", b'{"const": 0.1}', b"0.1", True),
        # `integer` must still mean integer once numbers stop being floats: 4.0
        # is an integral decimal and the draft admits it, 4.5 is not.
        ("an integral decimal is an integer", b'{"type": "integer"}', b"4.0", True),
        ("a fractional decimal is not", b'{"type": "integer"}', b"4.5", False),
    ),
    ids=(
        "adjacent-below",
        "adjacent-above",
        "overflow",
        "overflow-distinct",
        "fraction",
        "integral-decimal",
        "fractional-decimal",
    ),
)
def test_two_numbers_that_differ_stay_different(
    label: str, schema: bytes, instance: bytes, admitted: bool
) -> None:
    """A value whose identity is hashed cannot be collapsed by the decoder.

    Default float decoding maps two distinct JSON numbers onto one float64, so a
    value the pinned schema refuses would be admitted and durably written. The
    numbers are therefore decoded exactly, and a schema that pins one of them
    still refuses the other.
    """
    verdict = read_instance_document(instance, accepted(schema))

    assert isinstance(verdict, InstanceAccepted if admitted else InstanceRefused)


def test_the_named_place_orders_array_indexes_as_numbers_not_as_text() -> None:
    """Index 2 comes before index 10, which is what a reader expects to read."""
    listed = accepted(b'{"type": "array", "items": {"type": "string"}}')
    twelve = b"[" + b'"a",' * 2 + b"7," + b'"a",' * 7 + b"7]"

    verdict = read_instance_document(twelve, listed)

    assert isinstance(verdict, InstanceRefused)
    assert verdict.subject is not None
    assert verdict.subject.startswith("/2:"), verdict.subject


def test_a_key_that_contains_the_pointer_characters_is_unambiguous() -> None:
    """`a/b` and `a~b` are different keys, and the named place must say which."""
    keyed = accepted(b'{"type": "object", "properties": {"a/b~c": {"type": "string"}}}')

    verdict = read_instance_document(b'{"a/b~c": 7}', keyed)

    assert isinstance(verdict, InstanceRefused)
    assert verdict.subject is not None
    assert verdict.subject.startswith("/a~1b~0c:"), verdict.subject
