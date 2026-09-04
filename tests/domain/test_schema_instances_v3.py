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
    declared_instance_in_answer,
    instance_for_schema,
    read_authored_instance_document,
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


def test_a_violated_field_is_named_by_a_pointer_a_caller_can_wire_without_parsing() -> (
    None
):
    """A caller that wants one field, not prose, reads `violation` instead.

    `subject` stays the human sentence; `violation.pointer` is the exact RFC
    6901 pointer `subject` was built from, and `violation.reason` its message
    alone -- so a caller that wants `invalid_fields` never parses `subject`.
    """
    verdict = read_instance_document(b'{"name": "lasagne", "portions": 0}', A_MEAL)

    assert isinstance(verdict, InstanceRefused)
    assert verdict.violation is not None
    assert verdict.violation.pointer == "/portions"
    assert verdict.violation.reason == "0 is less than the minimum of 1"


def test_a_violation_with_no_addressable_field_names_no_pointer() -> None:
    """A `required` violation at the value's own root has no RFC 6901 pointer.

    The missing key is not a place in the document, so a caller asking for
    `violation.pointer` reads `None` rather than an empty string that would
    misname the value's root as an addressable field.
    """
    verdict = read_instance_document(b'{"portions": 0, "surprise": true}', A_MEAL)

    assert isinstance(verdict, InstanceRefused)
    assert verdict.violation is not None
    assert verdict.violation.pointer is None
    assert verdict.violation.reason == "'name' is a required property"


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


def test_an_exact_number_beyond_the_profile_is_refused_by_name() -> None:
    """A valid JSON number must return a verdict, never escape the decoder."""
    literal = b"1e" + b"9" * 19

    verdict = read_instance_document(literal, accepted(b"true"))

    assert isinstance(verdict, InstanceRefused)
    assert verdict.refusal is InstanceRefusal.NON_CANONICAL_NUMBER
    assert verdict.subject == literal.decode()


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


@pytest.mark.parametrize(
    ("schema", "instance", "admitted"),
    (
        (b'{"multipleOf": 1e-100000}', b"1e100000", True),
        (b'{"multipleOf": 2e-100000}', b"3e-100000", False),
        (b'{"multipleOf": 0.1}', b"0.3", True),
        (b'{"multipleOf": 0.2}', b"0.3", False),
        (b'{"multipleOf": 0.5}', b"4", True),
        (b'{"multipleOf": 0.5}', b"-4", True),
        (b'{"multipleOf": 1e999999999999999999}', b"0", True),
    ),
    ids=(
        "extreme-divisible",
        "extreme-nondivisible",
        "decimal-divisible",
        "decimal-nondivisible",
        "mixed-integer-decimal",
        "negative",
        "zero",
    ),
)
def test_multiple_of_is_exact_and_independent_of_decimal_context(
    schema: bytes, instance: bytes, admitted: bool
) -> None:
    verdict = read_instance_document(instance, accepted(schema))

    assert isinstance(verdict, InstanceAccepted if admitted else InstanceRefused)
    if isinstance(verdict, InstanceRefused):
        assert verdict.refusal is InstanceRefusal.SCHEMA_VIOLATED


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


@pytest.mark.parametrize(
    ("wrapper", "carried"),
    [
        pytest.param("{answer}", True, id="bare"),
        pytest.param("{answer}\n", True, id="trailing newline"),
        pytest.param("  \n {answer} \n ", True, id="surrounding whitespace"),
        pytest.param("Here is the report:\n{answer}", True, id="leading prose"),
        pytest.param("{answer}\nThat is everything.", True, id="trailing prose"),
        pytest.param("```json\n{answer}\n```", True, id="fenced"),
        pytest.param("```\n{answer}\n```\nDone.", True, id="fenced and explained"),
        pytest.param(
            "I read the brief {carefully} and then answered:\n{answer}",
            False,
            id="an opener in the prose before it",
        ),
    ],
)
def test_a_declared_value_is_found_wherever_prose_wrapped_it(
    wrapper: str, carried: bool
) -> None:
    """The shapes one model really returned for one brief, and the rule's own edge.

    A provider asked in prose for bare JSON answers in prose, so the same brief
    came back bare, fenced, or introduced by a sentence (#663). All of those
    carry the value; a sentence that opens a container of its own before the
    answer does not, because the rule reads exactly one document at the first
    opener rather than searching every one of them.
    """
    meal = '{"name": "lasagne", "portions": 4}'
    answer = wrapper.replace("{answer}", meal).encode("utf-8")

    found = declared_instance_in_answer(answer, A_MEAL)

    if not carried:
        assert found is None
        return
    assert found is not None
    assert read_instance_document(found, A_MEAL) == InstanceAccepted(
        {"name": "lasagne", "portions": 4}
    )


def test_an_answer_carrying_no_value_the_schema_admits_is_answered_with_nothing() -> (
    None
):
    """Narrowing never repairs: a wrapped value of the wrong shape stays refused."""
    assert declared_instance_in_answer(b"I could not do that.", A_MEAL) is None
    assert declared_instance_in_answer(b'Sorry:\n{"name": "lasagne"}\n', A_MEAL) is None


def test_a_narrowed_value_is_still_held_to_the_profile_the_whole_answer_is() -> None:
    """Extraction only proposes bytes; the profile alone decides about them."""
    duplicated = b'Here:\n{"name": "a", "name": "b", "portions": 1}'

    assert declared_instance_in_answer(duplicated, A_MEAL) is None


# `read_authored_instance_document` judges a value a caller authored -- an
# order a start supplies, or an answer a person types at a wait -- rather than
# one an executor produced. `read_instance_document` stays the produced-value
# door and is unaffected: these tests pin exactly where the two disagree.

A_NONEMPTY_STRING = accepted(b'{"type": "string", "minLength": 1}')
AN_UNBOUNDED_STRING = accepted(b'{"type": "string"}')


def test_an_authored_string_schema_value_reads_the_artifacts_raw_text() -> None:
    """The bytes ARE the string: no second, JSON-quoting layer is owed."""
    verdict = read_authored_instance_document(b"stir gently", A_NONEMPTY_STRING)

    assert verdict == InstanceAccepted("stir gently")


def test_an_authored_string_schema_value_still_enforces_minlength() -> None:
    """Reading raw text does not skip the schema the author still pinned."""
    verdict = read_authored_instance_document(b"", A_NONEMPTY_STRING)

    assert isinstance(verdict, InstanceRefused)
    assert verdict.refusal is InstanceRefusal.SCHEMA_VIOLATED


def test_an_authored_object_schema_value_still_demands_json() -> None:
    """Only a `\"string\"`-typed schema changes; every other shape is unaffected."""
    accepted_value = read_authored_instance_document(
        b'{"name": "lasagne", "portions": 4}', A_MEAL
    )
    refused_value = read_authored_instance_document(b"lasagne", A_MEAL)

    assert accepted_value == InstanceAccepted({"name": "lasagne", "portions": 4})
    assert isinstance(refused_value, InstanceRefused)
    assert refused_value.refusal is InstanceRefusal.INSTANCE_NOT_JSON


def test_a_json_encoded_string_stays_a_string_that_carries_its_own_quotes() -> None:
    """The chosen rule, pinned: a quoted JSON string is a string, quotes and all.

    Atelier owes no second reading that would strip a quoting an author never
    asked for (prototype rule, `docs/PRODUCT.md`) -- an author who means the
    bare word writes the bare word.
    """
    verdict = read_authored_instance_document(b'"hi"', AN_UNBOUNDED_STRING)

    assert verdict == InstanceAccepted('"hi"')


def test_the_produced_value_door_keeps_demanding_json_for_a_string_schema() -> None:
    """`read_instance_document` (agent/node output) is untouched by this rule.

    A produced value already arrives under its own executor's JSON-encoding
    promise (`atelier2.adapters.claude_subscription`'s StructuredOutput
    wrap/unwrap, #1061), so the same bytes read differently through the two
    doors on purpose.
    """
    quoted = read_instance_document(b'"hi"', AN_UNBOUNDED_STRING)
    bare = read_instance_document(b"hi", AN_UNBOUNDED_STRING)

    assert quoted == InstanceAccepted("hi")
    assert isinstance(bare, InstanceRefused)
    assert bare.refusal is InstanceRefusal.INSTANCE_NOT_JSON


@pytest.mark.parametrize(
    ("label", "instance", "expected"),
    (
        ("not utf-8", b"\xff", InstanceRefusal.INSTANCE_NOT_UTF8),
        (
            "a byte order mark",
            "﻿stir gently".encode(),
            InstanceRefusal.INSTANCE_CARRIES_BYTE_ORDER_MARK,
        ),
    ),
    ids=("utf8", "bom"),
)
def test_an_authored_string_schema_value_still_refuses_broken_bytes(
    label: str, instance: bytes, expected: InstanceRefusal
) -> None:
    """A `\"string\"` schema reads raw text, not raw bytes: it still owes UTF-8."""
    verdict = read_authored_instance_document(instance, A_NONEMPTY_STRING)

    assert isinstance(verdict, InstanceRefused)
    assert verdict.refusal is expected


def test_an_invalid_utf8_refusal_names_the_broken_byte_offset() -> None:
    """The ruled sentence is the place, not only the reason: a person cannot fix
    "invalid start byte" without knowing where in the artifact it broke."""
    verdict = read_instance_document(
        b'{"name": "stir \xff gently", "portions": 1}', A_MEAL
    )

    assert isinstance(verdict, InstanceRefused)
    assert verdict.refusal is InstanceRefusal.INSTANCE_NOT_UTF8
    assert verdict.subject == "invalid start byte at byte 15"


def test_an_authored_string_schema_refusal_also_names_the_broken_byte_offset() -> None:
    verdict = read_authored_instance_document(b"stir \xff gently", A_NONEMPTY_STRING)

    assert isinstance(verdict, InstanceRefused)
    assert verdict.refusal is InstanceRefusal.INSTANCE_NOT_UTF8
    assert verdict.subject == "invalid start byte at byte 5"


def test_instance_for_schema_is_the_one_dispatch_both_checks_agree_are_bound() -> None:
    """The decode-only unit `read_authored_instance_document` is built on.

    Exercised directly so the type dispatch is pinned independently of the
    bound and violation checks layered on top of it.
    """
    assert instance_for_schema(b"stir gently", A_NONEMPTY_STRING.schema) == (
        "stir gently"
    )
    assert instance_for_schema(
        b'{"name": "lasagne", "portions": 4}', A_MEAL.schema
    ) == {"name": "lasagne", "portions": 4}
